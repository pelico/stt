"""阿里云百炼实时语音识别。"""
import logging
import threading
import time
from typing import Optional

import dashscope
from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult

from app.asr.base import BaseASR
from app.config import settings
from app.diagnostics import describe_exception, metrics

logger = logging.getLogger("stt.asr")


class _Callback(RecognitionCallback):
    """收集识别文字并标记完成。"""

    def __init__(self):
        self.text = ""
        self.done = threading.Event()
        self.opened = threading.Event()
        self.error: Optional[str] = None

    def on_open(self):
        self.opened.set()

    def on_event(self, result: RecognitionResult):
        sentence = result.get_sentence()
        sentences = sentence if isinstance(sentence, list) else [sentence]
        for s in sentences:
            if not isinstance(s, dict):
                continue
            text = s.get("text", "")
            # end_time 不为 None 表示一句话结束
            if text and s.get("end_time") is not None:
                self.text += text

    def on_complete(self):
        self.done.set()

    def on_error(self, result: RecognitionResult):
        self.error = getattr(result, "message", str(result))
        self.done.set()

    def on_close(self):
        self.done.set()


class AliyunASR(BaseASR):
    engine_name = "aliyun"

    def __init__(self, cfg):
        self.api_key = cfg.get("dashscope_api_key")
        self.model = cfg.get("asr_model") or "paraformer-realtime-v2"
        self.language = cfg.get("asr_language") or "auto"
        self.sample_rate = 16000

    async def recognize(self, pcm: bytes) -> str:
        if not self.api_key:
            raise RuntimeError("未配置 DASHSCOPE_API_KEY")

        dashscope.api_key = self.api_key
        callback = _Callback()
        kwargs = {}
        if self.language != "auto" and "v2" in self.model:
            # paraformer-realtime-v2 及以后支持 language_hints，可避免中英混淆
            # v1 仅支持中文，传该参数可能被服务端拒绝
            kwargs["language_hints"] = [self.language]
        rt = Recognition(
            model=self.model,
            callback=callback,
            format="pcm",
            sample_rate=self.sample_rate,
            **kwargs,
        )

        req = metrics.next_id()
        duration = len(pcm) / (settings.sample_rate * 2)
        logger.info(
            "[ASR] req#%d → 阿里云百炼 %s | audio=%.1fs/%dKB "
            "timeout=(connect 5s, result 30s)",
            req, self.model, duration, len(pcm) // 1024,
        )
        started = time.monotonic()
        t_connected = t_sent = started
        try:
            rt.start()
            if not callback.opened.wait(timeout=5):
                rt.stop()
                raise TimeoutError("连接阶段 5s 未建立会话")
            t_connected = time.monotonic()

            chunk_size = self.sample_rate // 10 * 2  # 100ms
            for i in range(0, len(pcm), chunk_size):
                rt.send_audio_frame(pcm[i : i + chunk_size])
            rt.stop()
            t_sent = time.monotonic()

            if not callback.done.wait(timeout=30):
                raise TimeoutError("结果等待 30s 未返回")
            if callback.error:
                raise RuntimeError(f"ASR 错误: {callback.error}")

            text = callback.text.strip()
            latency = time.monotonic() - started
            metrics.record(
                True, latency, slow=latency * 1000 > settings.asr_slow_ms,
                url="dashscope",
            )
            logger.info(
                "[ASR] req#%d ✓ %.2fs (connect %.2fs, send %.2fs, wait %.2fs) "
                "text=%d字: %s",
                req, latency, t_connected - started, t_sent - t_connected,
                time.monotonic() - t_sent, len(text), text[:80].replace("\n", " "),
            )
            return text
        except Exception as e:
            latency = time.monotonic() - started
            info = describe_exception(e)
            if isinstance(e, TimeoutError):
                info["code"] = (
                    "connect_timeout" if "连接" in str(e) else "read_timeout"
                )
                info["hint"] = (
                    "连接阶段未建立：查网络与百炼服务状态"
                    if info["code"] == "connect_timeout"
                    else "结果等待超时：查百炼服务状态或调大等待时间"
                )
            metrics.record(
                False, latency, info["code"], info["detail"], info["hint"],
                url="dashscope",
            )
            logger.error(
                "[ASR] req#%d ✗ %s after %.2fs (connect %.2fs, send %.2fs) | %s | 建议: %s",
                req, info["code"], latency,
                t_connected - started, max(0.0, t_sent - t_connected),
                info["detail"], info["hint"],
            )
            # 超时也要上抛，否则会被当成「空结果」，既不重试也不熔断
            raise
