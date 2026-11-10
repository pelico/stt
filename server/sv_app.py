#!/usr/bin/env python3
"""SenseVoice-Small OpenAI-compatible STT adapter (server side, GCP).

Serves POST /v1/audio/transcriptions (multipart) using sherpa-onnx + SenseVoice int8.

v2 改进（2026-11-10）：
  1. 固定中文提示 language="zh"，显著减少纯噪声时蹦出英文单词的幻觉；
  2. 语气词/单字/重复单字结果直接返回空文本，让调用方天然丢弃；
  3. 若运行时版本把 SenseVoice 事件标签保留在 result.text 里，则顺手判非语音事件。
     注意：sherpa-onnx v2.0.1 的 ConvertSenseVoiceResult 会跳过前 4 个 token
     （语种/情感/事件/ITN），因此该版本拿不到事件标签，此项属于前瞻兼容。
     真正的非语音拦截依赖 rtsp-stt 端的 VAD 能量/占比门限。
以上过滤可用 SV_FILTER=0 关闭，语种可用 SV_LANGUAGE=auto 恢复自动。

v3 改进（2026-11-10）：
  推理移入线程池（SV_WORKERS，默认 2），事件循环不再被推理阻塞：
  - 单请求延迟不变（num_threads=1，单请求仍占 1 核）；
  - 并发请求可同时各占 1 核，2 vCPU 上吞吐接近翻倍；
  - onnxruntime 的 session.Run 线程安全（sherpa-onnx 自家 offline
    websocket server 就是单 recognizer 多线程并发 decode）。
  内存几乎不变（模型只有一份）。表现异常时设 SV_WORKERS=1 退回串行。
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import sherpa_onnx
from fastapi import FastAPI, File, Form, UploadFile

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sensevoice")

MODEL_DIR = os.environ.get(
    "SV_MODEL_DIR",
    "/home/administrator/sensevoice/models/sherpa-onnx-sense-voice-zh-en-ja-yue-2024-07-17",
)
# 中文场景固定 zh；设为空字符串恢复自动语种
SV_LANGUAGE = os.environ.get("SV_LANGUAGE", "zh")
FILTER_ENABLED = os.environ.get("SV_FILTER", "1") != "0"
# 并发推理线程数：2 vCPU 机器建议 2（每请求 num_threads=1，正好各占一核）
SV_WORKERS = max(1, int(os.environ.get("SV_WORKERS", "2")))

recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=os.path.join(MODEL_DIR, "model.int8.onnx"),
    tokens=os.path.join(MODEL_DIR, "tokens.txt"),
    use_itn=True,
    language=SV_LANGUAGE,
    num_threads=1,
)
# 每个线程完整跑「ffmpeg 转码 + 读样本 + 推理」，互不共享可变状态
_pool = ThreadPoolExecutor(max_workers=SV_WORKERS, thread_name_prefix="asr")

TAG_RE = re.compile(r"<\|[^|]*\|>")
PUNCT_RE = re.compile(r"[\s，。！？、,.!?~…:：;；\"'“”‘’()（）\-—_/\\|]+")
FILLER_CHARS = set("嗯啊哦呃唉哎呵哈嘿唔噢喂呀呐呗嘛额呣嘻嘘咯喽嘞")

# SenseVoice 的事件标签；除 Speech 外都视为非语音
EVENT_TAGS = {
    "Speech", "Music", "Bgm", "Applause", "Laughter", "Cry",
    "Cough", "Sneeze", "Breath", "Noise", "Silence", "Other", "Event",
}

app = FastAPI(title="sensevoice-stt")


@app.get("/v1/models")
@app.get("/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {"id": "sensevoice-small", "object": "model", "owned_by": "local"},
            {"id": "whisper-1", "object": "model", "owned_by": "local"},
        ],
    }


@app.get("/health")
def health():
    return {"status": "ok", "workers": SV_WORKERS}


def _to_wav16k(src: str) -> str:
    dst = src + ".16k.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-ac", "1", "-ar", "16000", "-vn", dst],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return dst


def _read_samples(path: str):
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return rate, samples


def _is_filler(text: str) -> bool:
    """纯语气词 / 单字 / 重复单字，视为噪声幻觉。"""
    cleaned = PUNCT_RE.sub("", text or "")
    if not cleaned:
        return True
    if len(cleaned) <= 1:
        return True
    if all(ch in FILLER_CHARS for ch in cleaned):
        return True
    if len(set(cleaned)) == 1:
        return True
    return False


def _transcribe_sync(src: str) -> str:
    """完整的转码 + 推理 + 过滤流水线，在线程池里跑（CPU 密集）。"""
    wav = _to_wav16k(src)
    try:
        rate, samples = _read_samples(wav)
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
    if samples.size == 0:
        return ""

    started = time.monotonic()
    stream = recognizer.create_stream()
    stream.accept_waveform(rate, samples)
    recognizer.decode_stream(stream)
    raw = (stream.result.text or "").strip()
    logger.info(
        "推理 %.2fs | %dB 音频 | raw=%r",
        time.monotonic() - started, samples.size * 2, raw,
    )

    tags = [t[2:-2] for t in TAG_RE.findall(raw)]
    events = [t for t in tags if t in EVENT_TAGS]
    text = TAG_RE.sub("", raw).strip()

    if FILTER_ENABLED:
        # v2.0.1 会丢弃标签 token，events 通常为空；保留此判断以兼容未来版本
        if events and not all(e == "Speech" for e in events):
            logger.info("丢弃非语音事件: %s", events)
            return ""
        if _is_filler(text):
            logger.info("丢弃语气词/单字结果: %r", text)
            return ""

    return text


@app.post("/v1/audio/transcriptions")
@app.post("/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form("sensevoice-small"),
    language: str = Form("auto"),
    prompt: str = Form(""),
):
    suffix = os.path.splitext(file.filename or "audio")[1] or ".bin"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        src = tmp.name
    try:
        try:
            text = await asyncio.get_running_loop().run_in_executor(
                _pool, _transcribe_sync, src
            )
        except subprocess.CalledProcessError:
            return {"text": "", "error": "invalid audio data"}
        return {"text": text}
    finally:
        try:
            os.remove(src)
        except OSError:
            pass
