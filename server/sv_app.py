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

v3.1 改进（2026-11-12，针对 1G 内存小机的 swap 抖动）：
  1. mlockall：默认把本进程全部内存钉死在物理 RAM（SV_MLOCK=0 关闭），
     内核内存紧张时只能换出其他进程——即「模型常驻 RAM，别人去 swap」。
     需要 systemd 单元里 LimitMEMLOCK=infinity，否则锁定失败退回普通模式。
  2. WAV 快路径：rtsp-stt 客户端发的本来就是 16kHz 单声道 s16le WAV，
     直接用 wave 模块读取，跳过每次请求的 ffmpeg 转码子进程（内存尖峰
     源头之一）；只有非该格式的输入才走 ffmpeg。

v3.2 改进（2026-11-12，可切换小模型）：
  SV_ASR_TYPE=sensevoice（默认）| zipformer：
  - zipformer 用 sherpa-onnx-streaming-zipformer-small-bilingual-zh-en
    （int8 总体积 ~90MB，RSS 峰值 ~150-250MB），中英双语，推理更快；
    流式模型按「整段一次性喂入 + input_finished」的方式当离线用。
  - 准确率比 SenseVoice 降一档，且输出无标点；安静清晰语音差距不大，
    嘈杂场景误识会增多。SV_ASR_TYPE=sensevoice 随时切回。
"""
import asyncio
import ctypes
import glob
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import sherpa_onnx
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sensevoice")

MODEL_DIR = os.environ.get(
    "SV_MODEL_DIR",
    "/home/administrator/sensevoice/models/sherpa-onnx-sense-voice-zh-en-ja-yue-2024-07-17",
)
# 识别引擎：sensevoice（默认，最准）| zipformer（小模型，省内存）
SV_ASR_TYPE = os.environ.get("SV_ASR_TYPE", "sensevoice").lower()
ZIPFORMER_DIR = os.environ.get(
    "SV_ZIPFORMER_DIR",
    "/home/administrator/sensevoice/models/"
    "sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16",
)
# 中文场景固定 zh；设为空字符串恢复自动语种
SV_LANGUAGE = os.environ.get("SV_LANGUAGE", "zh")
FILTER_ENABLED = os.environ.get("SV_FILTER", "1") != "0"
# 并发推理线程数：2 vCPU 机器建议 2（每请求 num_threads=1，正好各占一核）
SV_WORKERS = max(1, int(os.environ.get("SV_WORKERS", "2")))


def _mlock_all() -> None:
    """把本进程全部内存（含之后加载的模型）钉在物理 RAM。

    1G 小机上模型被换出到 swap 后，每次推理都从磁盘换页回内存
    （实测 si 300MB/s、iowait 95%、load 14），请求 45 秒超时。
    mlockall 后内核只能换出其他进程——正是「模型优先驻留 RAM」语义。
    需要 systemd LimitMEMLOCK=infinity；失败则记日志退回普通模式。
    """
    if os.environ.get("SV_MLOCK", "1") == "0":
        logger.info("SV_MLOCK=0，跳过内存锁定")
        return
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        MCL_CURRENT, MCL_FUTURE = 1, 2
        if libc.mlockall(MCL_CURRENT | MCL_FUTURE) == 0:
            logger.info("mlockall 成功：模型内存将常驻 RAM，不会被换出到 swap")
        else:
            err = ctypes.get_errno()
            logger.warning(
                "mlockall 失败（errno=%d，多半是 RLIMIT_MEMLOCK 太小），"
                "请在 systemd 单元加 LimitMEMLOCK=infinity 后重启",
                err,
            )
    except Exception as e:  # 锁定失败不影响服务可用性
        logger.warning("mlockall 异常: %s（继续普通模式）", e)


_mlock_all()

def _pick_model(directory: str, stem: str) -> str:
    """按前缀匹配 encoder/decoder/joiner（实际文件名带 epoch 版本号，如
    encoder-epoch-99-avg-1.onnx）；优先 int8 量化文件，其次 fp32。"""
    int8 = sorted(glob.glob(os.path.join(directory, f"{stem}*.int8.onnx")))
    plain = [
        p
        for p in sorted(glob.glob(os.path.join(directory, f"{stem}*.onnx")))
        if not p.endswith(".int8.onnx")
    ]
    for path in int8 + plain:
        return path
    raise FileNotFoundError(f"{directory} 下找不到 {stem}*.onnx")


if SV_ASR_TYPE == "zipformer":
    # 流式 transducer 模型按离线用法：整段喂入 + input_finished，一次出全句
    recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
        encoder=_pick_model(ZIPFORMER_DIR, "encoder"),
        decoder=_pick_model(ZIPFORMER_DIR, "decoder"),
        joiner=_pick_model(ZIPFORMER_DIR, "joiner"),
        tokens=os.path.join(ZIPFORMER_DIR, "tokens.txt"),
        num_threads=1,
        sample_rate=16000,
        decoding_method="modified_beam_search",
    )
    ASR_TYPE = "zipformer"
else:
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=os.path.join(MODEL_DIR, "model.int8.onnx"),
        tokens=os.path.join(MODEL_DIR, "tokens.txt"),
        use_itn=True,
        language=SV_LANGUAGE,
        num_threads=1,
    )
    ASR_TYPE = "sensevoice"
logger.info("ASR 引擎: %s", ASR_TYPE)
# 每个线程完整跑「转码 + 读样本 + 推理」，互不共享可变状态
_pool = ThreadPoolExecutor(max_workers=SV_WORKERS, thread_name_prefix="asr")

# 排队上限：单 worker 时，第 1 个在推理、其余在队列里挂着连接干等。
# 客户端超时重试会在这里堆积成活锁（超时→重试→排队→更慢→再超时）。
# 排队数超上限直接回 503，客户端收到后按退避节奏重发，不再白挂连接。
SV_MAX_PENDING = max(1, int(os.environ.get("SV_MAX_PENDING", "3")))
_pending = 0
_pending_lock = threading.Lock()

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
    return {"status": "ok", "workers": SV_WORKERS, "asr_type": ASR_TYPE}


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


def _direct_wav_samples(path: str):
    """输入已是 16kHz 单声道 s16le WAV 时直接读样本，返回 None 则走 ffmpeg。

    rtsp-stt 客户端上传的就是这种格式——跳过每次请求的 ffmpeg 子进程，
    1G 小机上子进程 fork+编码是每次请求的内存尖峰源头之一。
    """
    try:
        with wave.open(path, "rb") as w:
            if (
                w.getframerate() == 16000
                and w.getnchannels() == 1
                and w.getsampwidth() == 2
            ):
                raw = w.readframes(w.getnframes())
                samples = (
                    np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                )
                return 16000, samples
    except (wave.Error, EOFError, PermissionError, FileNotFoundError):
        pass
    return None


def _transcribe_sync(src: str) -> str:
    """完整的转码 + 推理 + 过滤流水线，在线程池里跑（CPU 密集）。"""
    direct = _direct_wav_samples(src)
    if direct is not None:
        rate, samples = direct
    else:
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
    if ASR_TYPE == "zipformer":
        stream = recognizer.create_stream()
        stream.accept_waveform(rate, samples)
        stream.input_finished()
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        raw = (recognizer.get_result(stream) or "").strip()
    else:
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
    global _pending
    with _pending_lock:
        if _pending >= SV_MAX_PENDING:
            logger.warning("排队满（%d/%d），拒绝请求回 503", _pending, SV_MAX_PENDING)
            return JSONResponse(status_code=503, content={"error": "busy"})
        _pending += 1
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
        with _pending_lock:
            _pending -= 1
        try:
            os.remove(src)
        except OSError:
            pass
