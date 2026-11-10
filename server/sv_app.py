#!/usr/bin/env python3
"""SenseVoice-Small OpenAI-compatible STT adapter (server side, GCP).

Serves POST /v1/audio/transcriptions (multipart) using sherpa-onnx + SenseVoice int8.

v2 改进（2026-11-10）：
  1. 固定中文提示 language="zh"，显著减少纯噪声时蹦出英文单词的幻觉；
  2. 解析 SenseVoice 事件标签，非 Speech（音乐/咳嗽/呼吸/噪声等）直接返回空文本；
  3. 语气词/单字/重复单字结果直接返回空文本，让调用方天然丢弃。
以上均可用环境变量关闭，方便对照排查。
"""
import logging
import os
import re
import shutil
import subprocess
import tempfile
import wave

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

recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=os.path.join(MODEL_DIR, "model.int8.onnx"),
    tokens=os.path.join(MODEL_DIR, "tokens.txt"),
    use_itn=True,
    language=SV_LANGUAGE,
)

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
    return {"status": "ok"}


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
    wav = None
    try:
        try:
            wav = _to_wav16k(src)
        except subprocess.CalledProcessError:
            return {"text": "", "error": "invalid audio data"}
        rate, samples = _read_samples(wav)
        if samples.size == 0:
            return {"text": ""}

        stream = recognizer.create_stream()
        stream.accept_waveform(rate, samples)
        recognizer.decode_stream(stream)
        raw = (stream.result.text or "").strip()

        tags = [t[2:-2] for t in TAG_RE.findall(raw)]
        events = [t for t in tags if t in EVENT_TAGS]
        text = TAG_RE.sub("", raw).strip()
        logger.info("raw=%r tags=%s -> %r", raw, tags, text)

        if FILTER_ENABLED:
            # 事件标签是最可靠的非语音判据（咳嗽/呼吸/音乐/噪声）
            if events and not all(e == "Speech" for e in events):
                logger.info("丢弃非语音事件: %s", events)
                return {"text": ""}
            if _is_filler(text):
                logger.info("丢弃语气词/单字结果: %r", text)
                return {"text": ""}

        return {"text": text}
    finally:
        for p in (src, wav):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
