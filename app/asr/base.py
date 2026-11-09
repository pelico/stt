"""ASR 引擎基类与通用工具。"""
import io
import wave
from abc import ABC, abstractmethod


class BaseASR(ABC):
    """所有 ASR 引擎的统一接口：输入 16kHz 单声道 PCM，返回文字。"""

    engine_name = "base"

    @abstractmethod
    async def recognize(self, pcm: bytes) -> str:
        """识别一段 PCM 音频。"""
        raise NotImplementedError


def pcm_to_wav(pcm: bytes, sample_rate: int = 16000) -> bytes:
    """把裸 PCM(s16le/单声道) 封装成 WAV。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()
