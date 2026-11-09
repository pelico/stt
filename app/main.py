"""FastAPI 应用入口。"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db, save_transcript, list_transcripts, iso_utc
from app.stream import AudioStreamer
from app.runtime_config import get_runtime, ENGINES
from app.asr import get_asr

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("stt")

# 活跃的 WebSocket 连接
_active_clients: List[WebSocket] = []

# 拉流任务引用
_streamer: AudioStreamer | None = None
_stream_task: asyncio.Task | None = None


def _engine_ready(cfg) -> bool:
    engine = cfg.get("asr_engine")
    if engine == "aliyun":
        return bool(cfg.get("dashscope_api_key"))
    if engine == "tencent":
        return bool(cfg.get("tencent_secret_id") and cfg.get("tencent_secret_key"))
    if engine == "openai":
        return bool(cfg.get("openai_base_url") and cfg.get("openai_api_key"))
    return False


async def _broadcast(message: dict):
    """向所有前端推送消息，单个客户端卡住不影响音频处理。"""
    for ws in list(_active_clients):
        try:
            await asyncio.wait_for(ws.send_json(message), timeout=1)
        except Exception:
            pass


async def on_speech_segment(audio_data: bytes):
    """收到一段语音后的处理：识别 -> 存库 -> 推送前端。"""
    cfg = get_runtime()
    duration_sec = len(audio_data) / (settings.sample_rate * 2)
    logger.info("收到语音段: %.1f 秒", duration_sec)

    try:
        text = await get_asr(cfg).recognize(audio_data)
    except Exception as e:
        logger.error("ASR 识别失败: %s", e)
        text = ""

    if not text:
        logger.debug("识别结果为空，跳过")
        return

    logger.info("识别结果: %s", text)

    try:
        record = save_transcript(
            text=text,
            duration_sec=int(duration_sec),
            engine=cfg.get("asr_engine"),
        )
    except Exception as e:
        logger.error("保存数据库失败: %s", e)
        record = None

    message = {
        "type": "transcript",
        "text": text,
        "timestamp": iso_utc(record.timestamp) if record else None,
        "duration_sec": int(duration_sec),
        "id": record.id if record else None,
    }
    await _broadcast(message)


async def on_vad_state(state: dict):
    """把 VAD 实时状态推给前端，用于调参。"""
    await _broadcast(state)


def _on_config_change(changed: List[str]):
    """配置热更新回调。"""
    if "rtsp_url" in changed and _streamer is not None:
        logger.info("RTSP 地址已变更，重启拉流")
        _streamer.restart()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("数据库初始化完成: %s", settings.db_path)

    cfg = get_runtime()
    cfg.subscribe(_on_config_change)

    global _streamer, _stream_task
    _streamer = AudioStreamer(
        on_speech_segment=on_speech_segment,
        cfg=cfg,
        on_vad_state=on_vad_state,
    )
    _stream_task = asyncio.create_task(_streamer.start())
    logger.info(
        "音频拉流已启动，RTSP: %s，引擎: %s",
        cfg.get("rtsp_url"),
        cfg.get("asr_engine"),
    )

    yield

    if _streamer:
        _streamer.stop()
    if _stream_task:
        _stream_task.cancel()
        try:
            await _stream_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="RTSP STT", lifespan=lifespan)

static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(static_dir, "index.html"))


@app.get("/api/transcripts")
async def get_transcripts(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    keyword: str = Query(None),
):
    """查询历史识别记录。"""
    records = list_transcripts(limit=limit, offset=offset, keyword=keyword)
    return {"items": [r.to_dict() for r in records], "total": len(records)}


@app.get("/api/settings")
async def read_settings():
    """读取当前配置，敏感字段打码。"""
    cfg = get_runtime()
    return {
        "values": cfg.snapshot(mask=True),
        "engines": list(ENGINES),
        "engine_ready": _engine_ready(cfg),
    }


@app.post("/api/settings")
async def update_settings(patch: dict):
    """增量更新配置，立即生效。敏感字段留空表示不修改。"""
    cfg = get_runtime()
    try:
        changed = cfg.update(patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "changed": changed,
        "values": cfg.snapshot(mask=True),
        "engine_ready": _engine_ready(cfg),
    }


@app.get("/api/health")
async def health():
    cfg = get_runtime()
    return {
        "status": "ok",
        "rtsp_url": cfg.get("rtsp_url"),
        "asr_engine": cfg.get("asr_engine"),
        "engine_ready": _engine_ready(cfg),
        "stream_running": bool(_stream_task and not _stream_task.done()),
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _active_clients.append(ws)
    logger.info("WebSocket 客户端连接，当前 %d 个", len(_active_clients))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if ws in _active_clients:
            _active_clients.remove(ws)
        logger.info("WebSocket 客户端断开，剩余 %d 个", len(_active_clients))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.http_port,
        log_level=settings.log_level.lower(),
    )
