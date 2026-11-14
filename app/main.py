"""FastAPI 应用入口。"""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from urllib.parse import quote
from fastapi.staticfiles import StaticFiles

from app.audio_dump import dump_rejected
from app.auth import COOKIE_NAME, make_token, password_matches, verify_token
from app.config import settings
from app.database import (
    init_db, save_transcript, list_transcripts, count_transcripts, iso_utc,
)
from app.diagnostics import metrics
from app.logbuffer import BufferHandler, LogBuffer
from app.pending_store import parse_iso_naive
from app.rtsp_history import add_history, list_history, remove_history
from app.stream import AudioStreamer
from app.text_filter import noise_reason
from app.runtime_config import get_runtime, ENGINES, asr_active, in_schedule
from app.asr import get_asr

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("stt")

# 供 Web 页面查看的运行日志（仅内存，重启清空）
log_buffer = LogBuffer(capacity=1000)
_buffer_handler = BufferHandler(log_buffer)
_buffer_handler.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
logging.getLogger().addHandler(_buffer_handler)


def _setup_file_logging():
    """排查超时往往是事后，内存缓冲重启即清空，因此额外轮转落盘。"""
    if not settings.log_file_enabled:
        return
    try:
        path = settings.log_file_path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=max(1, int(settings.log_file_max_mb)) * 1024 * 1024,
            backupCount=max(0, int(settings.log_file_backups)),
            encoding="utf-8",
        )
        handler.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(handler)
        logger.info(
            "运行日志已落盘: %s（单文件 %sMB × %s 份轮转）",
            path, settings.log_file_max_mb, settings.log_file_backups,
        )
    except Exception as e:
        logger.warning("运行日志落盘失败（不影响运行）: %s", e)


_setup_file_logging()

# 可选访问口令：空表示不启用（兼容原有部署）
ACCESS_TOKEN = (settings.access_token or "").strip()

# 登录失败限速：防止公网暴力试口令（全局计数，个人服务够用）
_login_fails = 0
_login_locked_until = 0.0
LOGIN_FAIL_LIMIT = 8
LOGIN_LOCK_SEC = 300

# 登录页与登录接口之外全部需要会话；/api/health 保持开放便于探活
PUBLIC_PATHS = ("/login", "/api/login", "/api/health", "/favicon.ico")


def _set_session_cookie(response, password: str, request: Request):
    """签发会话 Cookie。经 HTTPS（CF 代理）访问时加 Secure。"""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie(
        COOKIE_NAME,
        make_token(password),
        max_age=30 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=(proto == "https"),
        path="/",
    )


class WSClient:
    """每个 WebSocket 一个发送队列 + 独立发送任务。

    音频采集循环只做非阻塞投递，慢/半死的浏览器不会再拖住 VAD；
    也不再对 send 套 wait_for（超时取消可能写坏半帧，直接破坏协议）。
    """

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.listening = False
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.sender = asyncio.create_task(self._send_loop())

    async def _send_loop(self):
        try:
            while True:
                item = await self.queue.get()
                if isinstance(item, bytes):
                    await self.ws.send_bytes(item)
                else:
                    await self.ws.send_json(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 连接断开由 endpoint 的 finally 清理
            pass

    def offer(self, item) -> bool:
        """非阻塞投递；队列满时丢最旧的一条，保证实时性优先。"""
        try:
            self.queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(item)
                return True
            except Exception:
                return False

    def close(self):
        self.sender.cancel()


_clients: Dict[WebSocket, WSClient] = {}

# 拉流任务引用
_streamer: Optional[AudioStreamer] = None
_stream_task: Optional[asyncio.Task] = None

# 识别服务熔断：连续失败时退避，避免模型重载期间把串行队列占满
_asr_failures = 0
_asr_blocked_until = 0.0
ASR_FAIL_THRESHOLD = 3
ASR_BACKOFF_CAP = 60

# 音频留证并发上限，避免大量丢弃片段同时写 SD 卡
_dump_running = 0


def _engine_ready(cfg) -> bool:
    engine = cfg.get("asr_engine")
    if engine == "aliyun":
        return bool(cfg.get("dashscope_api_key"))
    if engine == "tencent":
        return bool(cfg.get("tencent_secret_id") and cfg.get("tencent_secret_key"))
    if engine == "openai":
        return bool(cfg.get("openai_base_url") and cfg.get("openai_api_key"))
    return False


def _broadcast(message: dict):
    """向所有前端投递消息（同步、非阻塞）。"""
    for client in list(_clients.values()):
        client.offer(message)


async def _dump_rejected(pcm: bytes, reason: str):
    """异步留存被过滤片段；开关默认关闭。"""
    global _dump_running
    if not get_runtime().get("save_rejected_audio", False):
        return
    if _dump_running >= 3:
        return
    _dump_running += 1
    try:
        name = await asyncio.to_thread(dump_rejected, pcm, reason)
        if name:
            logger.info("已留存被过滤音频: %s（%s）", name, reason)
    except Exception as e:
        logger.warning("留存被过滤音频失败: %s", e)
    finally:
        _dump_running -= 1


async def on_speech_segment(audio_data: bytes, wall_iso: Optional[str] = None) -> bool:
    """收到一段语音后的处理：识别 -> 存库 -> 推送前端。

    返回 True 表示该段已完成、可从待识别队列出队；False 表示识别失败，
    调用方应保留该段稍后重试（完整优先模式）。wall_iso 是采集时刻，
    补处理时用写入库，历史记录按「说话时间」而非「识别完成时间」排序。
    """
    global _asr_failures, _asr_blocked_until
    started = time.monotonic()
    cfg = get_runtime()
    duration_sec = len(audio_data) / (settings.sample_rate * 2)
    captured_at = parse_iso_naive(wall_iso)
    queue_lag = (
        (datetime.now(timezone.utc).replace(tzinfo=None) - captured_at).total_seconds()
        if captured_at else 0.0
    )
    backlog = _streamer.backlog if _streamer else 0
    # 只拦「手动暂停」：时段外拉流已停、不会有新段，但补转积压要照常进行，
    # 这里若按 asr_active 拦截，时段外的积压会被静音跳过并直接出队丢弃
    if not cfg.get("asr_enabled", True):
        logger.debug("识别总开关已关闭，跳过 %.1f 秒语音", duration_sec)
        return True
    # 开始明细降为 DEBUG：引擎/模式/连续失败都是静态或异常时才需要的信息，
    # 音频大小在 ASR 请求行里有，正常路径不重复打印
    logger.debug(
        "[段] 开始 | 音频 %.1fs/%dKB | 排队 %.1fs | 积压 %d | 引擎 %s | 模式 %s | 连续失败 %d",
        duration_sec, len(audio_data) // 1024, queue_lag, backlog,
        cfg.get("asr_engine"), _streamer.mode if _streamer else "-", _asr_failures,
    )

    now = time.monotonic()
    if now < _asr_blocked_until:
        logger.debug(
            "识别服务熔断中，%.0f 秒后重试", _asr_blocked_until - now
        )
        return False

    try:
        text = await get_asr(cfg).recognize(audio_data)
    except Exception as e:
        _asr_failures += 1
        logger.error(
            "[段] 失败 | 连续 %d/%d 次 | 耗时 %.2fs | 排队 %.1fs | 积压 %d | %s: %s",
            _asr_failures, ASR_FAIL_THRESHOLD, time.monotonic() - started,
            queue_lag, backlog, type(e).__name__, str(e)[:200],
        )
        if _asr_failures >= ASR_FAIL_THRESHOLD:
            backoff = min(
                ASR_BACKOFF_CAP,
                5 * (2 ** min(_asr_failures - ASR_FAIL_THRESHOLD, 4)),
            )
            _asr_blocked_until = time.monotonic() + backoff
            logger.warning("识别服务连续失败，熔断 %d 秒后探活", backoff)
            _broadcast({"type": "asr_state", "blocked": True, "retry_in": backoff})
        return False

    if _asr_failures:
        _asr_failures = 0
        _asr_blocked_until = 0.0
        logger.info("识别服务已恢复")
        _broadcast({"type": "asr_state", "blocked": False})

    if not text:
        logger.debug("识别结果为空，跳过")
        return True

    if cfg.get("filter_noise_text", True):
        reason = noise_reason(text, cfg.get("filter_ignore_words", ""))
        if reason:
            logger.info("丢弃噪音识别结果 (%s): %s", reason, text)
            await _dump_rejected(audio_data, f"text-{reason}")
            return True

    lag = (
        (datetime.now(timezone.utc).replace(tzinfo=None) - captured_at).total_seconds()
        if captured_at else 0.0
    )
    prefix = "（延迟 %.0f 秒补处理）" % lag if lag > 30 else ""
    # 结果明细降为 DEBUG：耗时与文本已在 [ASR] ✓ 行，正常路径不重复
    logger.debug(
        "[段] 结果%s | 耗时 %.2fs | 排队 %.1fs | 积压 %d | %s",
        prefix, time.monotonic() - started, queue_lag, backlog,
        text.replace("\n", " ")[:120],
    )

    try:
        record = await asyncio.to_thread(
            save_transcript,
            text=text,
            duration_sec=int(duration_sec),
            engine=cfg.get("asr_engine"),
            timestamp=captured_at,
        )
    except Exception as e:
        logger.error("保存数据库失败: %s", e)
        record = None

    message = {
        "type": "transcript",
        "text": text,
        "timestamp": iso_utc(record.timestamp) if record else iso_utc(captured_at),
        "duration_sec": int(duration_sec),
        "id": record.id if record else None,
    }
    _broadcast(message)
    return True


async def on_vad_state(state: dict):
    """把 VAD 实时状态推给前端，用于调参（非阻塞）。"""
    _broadcast(state)


async def on_audio_frame(frame: bytes):
    """把实时 PCM 帧推给正在播放的客户端（非阻塞）。"""
    for client in list(_clients.values()):
        if client.listening:
            client.offer(frame)


async def on_stream_state(paused: bool):
    """时段外挂起/恢复时通知前端。"""
    _broadcast({"type": "stream", "paused": paused})


async def on_rejected_segment(pcm: bytes, reason: str):
    """VAD 层丢弃的片段，留证用于判断是噪声还是误杀。"""
    await _dump_rejected(pcm, f"vad-{reason}")


def _on_config_change(changed: List[str]):
    """配置热更新回调。"""
    if "rtsp_url" in changed and _streamer is not None:
        logger.info("RTSP 地址已变更，重启拉流")
        _streamer.restart()
    if "queue_mode" in changed:
        logger.warning(
            "识别队列模式已改为 %s，需重启容器后生效",
            get_runtime().get("queue_mode"),
        )


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
        on_audio_frame=on_audio_frame,
        should_stream=lambda: in_schedule(get_runtime()),
        on_stream_state=on_stream_state,
        on_rejected_segment=on_rejected_segment,
    )
    # 记录当前地址到历史，方便下次切换
    add_history(cfg.get("rtsp_url"))

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


@app.middleware("http")
async def access_guard(request: Request, call_next):
    """登录保护：未登录时页面跳 /login、API 回 401。

    兼容旧用法：X-Access-Token 头或 ?token= 仍被接受（口令正确时
    顺手把会话 Cookie 签上，老书签/老脚本不断档）。
    """
    path = request.url.path
    if not ACCESS_TOKEN or path in PUBLIC_PATHS:
        return await call_next(request)

    supplied_token = (
        request.headers.get("x-access-token")
        or request.query_params.get("token")
        or ""
    )
    if password_matches(supplied_token, ACCESS_TOKEN):
        response = await call_next(request)
        _set_session_cookie(response, ACCESS_TOKEN, request)
        return response

    cookie = request.cookies.get(COOKIE_NAME) or ""
    if verify_token(cookie, ACCESS_TOKEN):
        return await call_next(request)

    if path.startswith("/api/"):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    # 页面请求跳登录页，next 用于登录后回跳
    return RedirectResponse(f"/login?next={quote(path, safe='')}", status_code=307)


@app.post("/api/login")
async def login(request: Request):
    """口令登录，成功签发会话 Cookie。"""
    global _login_fails, _login_locked_until
    now = time.monotonic()
    if now < _login_locked_until:
        return JSONResponse(
            {"detail": "尝试次数过多，请稍后再试"},
            status_code=429,
            headers={"Retry-After": str(int(_login_locked_until - now))},
        )
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not password_matches(str(body.get("password") or ""), ACCESS_TOKEN):
        _login_fails += 1
        if _login_fails >= LOGIN_FAIL_LIMIT:
            _login_locked_until = time.monotonic() + LOGIN_LOCK_SEC
            _login_fails = 0
            logger.warning("口令连续错误 %d 次，锁定 %d 秒", LOGIN_FAIL_LIMIT, LOGIN_LOCK_SEC)
        return JSONResponse({"detail": "口令错误"}, status_code=401)
    _login_fails = 0
    response = JSONResponse({"ok": True})
    _set_session_cookie(response, ACCESS_TOKEN, request)
    logger.info("登录成功（%s）", request.client.host if request.client else "-")
    return response


@app.post("/api/logout")
async def logout():
    """清除会话。"""
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/login")
async def login_page():
    return FileResponse(
        os.path.join(static_dir, "login.html"),
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/")
async def index():
    # no-cache：浏览器每次都带 ETag 协商（命中仍 304），保证升级镜像后
    # 刷新页面就能拿到新版界面，不会拿旧 JS 还以为功能退回了
    return FileResponse(
        os.path.join(static_dir, "index.html"),
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/transcripts")
async def get_transcripts(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    keyword: str = Query(None),
):
    """查询历史识别记录；total 为匹配总数（不受分页影响）。"""
    records = await asyncio.to_thread(
        list_transcripts, limit=limit, offset=offset, keyword=keyword
    )
    total = await asyncio.to_thread(count_transcripts, keyword=keyword)
    return {"items": [r.to_dict() for r in records], "total": total}


@app.get("/api/settings")
async def read_settings():
    """读取当前配置，敏感字段打码。"""
    cfg = get_runtime()
    return {
        "values": cfg.snapshot(mask=True),
        "engines": list(ENGINES),
        "engine_ready": _engine_ready(cfg),
        "asr_active": asr_active(cfg),
    }


@app.get("/api/rtsp-history")
async def get_rtsp_history():
    """最近使用过的 RTSP 地址，最近的在最前。"""
    return {"items": list_history()}


@app.delete("/api/rtsp-history")
async def delete_rtsp_history(url: str = Query(...)):
    """删除一条历史地址。"""
    return {"items": remove_history(url)}


@app.post("/api/settings")
async def update_settings(patch: dict):
    """增量更新配置，立即生效。敏感字段留空表示不修改。"""
    cfg = get_runtime()
    try:
        changed = cfg.update(patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if "rtsp_url" in changed:
        add_history(cfg.get("rtsp_url"))
    return {
        "changed": changed,
        "values": cfg.snapshot(mask=True),
        "engine_ready": _engine_ready(cfg),
        "asr_active": asr_active(cfg),
    }


@app.get("/api/logs")
async def get_logs(
    after: int = Query(0, ge=0),
    limit: int = Query(300, ge=1, le=1000),
):
    """增量拉取运行日志。after 为上次拿到的最大 id。"""
    return {
        "items": log_buffer.since(after=after, limit=limit),
        "last_id": log_buffer.last_id,
    }


@app.delete("/api/logs")
async def clear_logs():
    """清空页面日志缓冲，不影响 docker logs。"""
    return {"cleared": True, "last_id": log_buffer.clear()}


@app.get("/api/health")
async def health():
    cfg = get_runtime()
    return {
        "status": "ok",
        "rtsp_url": cfg.get("rtsp_url"),
        "asr_engine": cfg.get("asr_engine"),
        "engine_ready": _engine_ready(cfg),
        "asr_enabled": cfg.get("asr_enabled"),
        "asr_schedule_enabled": cfg.get("asr_schedule_enabled"),
        "asr_start_time": cfg.get("asr_start_time"),
        "asr_end_time": cfg.get("asr_end_time"),
        "asr_active": asr_active(cfg),
        "asr_blocked": time.monotonic() < _asr_blocked_until,
        "stream_paused": bool(_streamer and _streamer.paused),
        "stream_running": bool(_stream_task and not _stream_task.done()),
        "queue_mode": _streamer.mode if _streamer else None,
        "queue_backlog": _streamer.backlog if _streamer else 0,
        "queue_backlog_bytes": _streamer.backlog_bytes if _streamer else 0,
        "queue_lag_sec": _streamer.lag_sec if _streamer else 0,
        "queue_totals": _streamer.queue_totals if _streamer else None,
        "diagnostics": metrics.snapshot(),
        "asr_timeout": settings.asr_timeout,
        "asr_connect_timeout": settings.asr_connect_timeout,
        "asr_slow_ms": settings.asr_slow_ms,
        "log_file": settings.log_file_path if settings.log_file_enabled else None,
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if ACCESS_TOKEN:
        # 会话 Cookie（同源 WS 握手自动携带）或兼容旧的 ?token= 口令
        cookie = ws.cookies.get(COOKIE_NAME) or ""
        supplied = (
            ws.query_params.get("token")
            or ws.headers.get("x-access-token")
            or ""
        )
        if not (
            verify_token(cookie, ACCESS_TOKEN)
            or password_matches(supplied, ACCESS_TOKEN)
        ):
            await ws.close(code=4401)
            return

    await ws.accept()
    client = WSClient(ws)
    _clients[ws] = client
    if _streamer is not None and _streamer.paused:
        client.offer({"type": "stream", "paused": True})
    logger.info("WebSocket 客户端连接，当前 %d 个", len(_clients))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") == "listen":
                client.listening = bool(msg.get("on"))
                logger.info(
                    "客户端%s实时播放，播放数 %d",
                    "开启" if client.listening else "关闭",
                    sum(1 for c in _clients.values() if c.listening),
                )
    except WebSocketDisconnect:
        pass
    finally:
        _clients.pop(ws, None)
        client.close()
        logger.info("WebSocket 客户端断开，剩余 %d 个", len(_clients))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.http_port,
        log_level=settings.log_level.lower(),
    )
