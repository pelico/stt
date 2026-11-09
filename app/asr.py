"""阿里云百炼实时语音识别封装。"""
import asyncio
import logging
import threading
from typing import Optional

import dashscope
from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult

from app.config import settings

logger = logging.getLogger(__name__)


class _STTCallback(RecognitionCallback):
    """dashscope 识别回调，收集文字并标记完成。"""

    def __init__(self):
        self.text = ""
        self.done = threading.Event()
        self.opened = threading.Event()
        self.error: Optional[str] = None

    def on_open(self):
        self.opened.set()

    def on_event(self, result: RecognitionResult):
        sentence = result.get_sentence()
        # sentence 可能是 dict 或 list
        sentences = sentence if isinstance(sentence, list) else [sentence]
        for s in sentences:
            if not isinstance(s, dict):
                continue
            text = s.get("text", "")
            end_time = s.get("end_time")
            # end_time 不为 None 表示一句话结束
            if text and end_time is not None:
                self.text += text

    def on_complete(self):
        self.done.set()

    def on_error(self, result: RecognitionResult):
        self.error = getattr(result, "message", str(result))
        self.done.set()

    def on_close(self):
        self.done.set()


class AliyunASR:
    """阿里云百炼实时语音识别。

    每段语音开一个实时转写会话，发完音频后停止并取回识别结果。
    """

    def __init__(self):
        dashscope.api_key = settings.dashscope_api_key
        self._model = settings.asr_model

    async def recognize(self, audio_data: bytes) -> str:
        """识别一段 PCM 音频，返回文字。"""
        if not settings.dashscope_api_key:
            logger.error("未配置 DASHSCOPE_API_KEY，跳过识别")
            return ""

        callback = _STTCallback()
        rt = Recognition(
            model=self._model,
            callback=callback,
            format="pcm",
            sample_rate=settings.sample_rate,
        )

        try:
            rt.start()
            # 等待连接建立（最多 5 秒）
            if not callback.opened.wait(timeout=5):
                logger.error("ASR 连接超时")
                rt.stop()
                return ""

            # 分块发送音频（每块约 100ms）
            chunk_size = settings.sample_rate // 10 * 2
            for i in range(0, len(audio_data), chunk_size):
                rt.send_audio_frame(audio_data[i : i + chunk_size])
            rt.stop()

            # 等待识别完成（最多 30 秒，一段语音通常几秒就出结果）
            if not callback.done.wait(timeout=30):
                logger.warning("ASR 识别超时")

            if callback.error:
                logger.error("ASR 错误: %s", callback.error)

            return callback.text.strip()
        except Exception as e:
            logger.error("ASR 会话异常: %s", e)
            return ""


# 全局 ASR 实例
_asr: Optional[AliyunASR] = None


def get_asr() -> AliyunASR:
    global _asr
    if _asr is None:
        _asr = AliyunASR()
    return _asr
