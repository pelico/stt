"""阿里云百炼实时语音识别。"""
import logging
import threading
from typing import Optional

import dashscope
from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult

from app.asr.base import BaseASR

logger = logging.getLogger(__name__)


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
        self.sample_rate = 16000

    async def recognize(self, pcm: bytes) -> str:
        if not self.api_key:
            logger.error("未配置 DASHSCOPE_API_KEY，跳过识别")
            return ""

        dashscope.api_key = self.api_key
        callback = _Callback()
        rt = Recognition(
            model=self.model,
            callback=callback,
            format="pcm",
            sample_rate=self.sample_rate,
        )

        try:
            rt.start()
            if not callback.opened.wait(timeout=5):
                logger.error("ASR 连接超时")
                rt.stop()
                return ""

            chunk_size = self.sample_rate // 10 * 2  # 100ms
            for i in range(0, len(pcm), chunk_size):
                rt.send_audio_frame(pcm[i : i + chunk_size])
            rt.stop()

            if not callback.done.wait(timeout=30):
                logger.warning("ASR 识别超时")
            if callback.error:
                logger.error("ASR 错误: %s", callback.error)

            return callback.text.strip()
        except Exception as e:
            logger.error("ASR 会话异常: %s", e)
            return ""
