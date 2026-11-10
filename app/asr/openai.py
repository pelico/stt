"""OpenAI 兼容的语音转写接口（/audio/transcriptions）。

可指向 OpenAI 官方、任意兼容中转服务，或本地兼容服务。
"""
import asyncio
import logging

import requests

from app.asr.base import BaseASR, pcm_to_wav
from app.config import settings

logger = logging.getLogger(__name__)


class OpenAISR(BaseASR):
    engine_name = "openai"

    def __init__(self, cfg):
        self.base_url = (cfg.get("openai_base_url") or "").rstrip("/")
        self.api_key = cfg.get("openai_api_key")
        self.model = cfg.get("openai_model") or "whisper-1"
        self.language = cfg.get("asr_language") or "auto"
        self.timeout = max(5, int(settings.asr_timeout))

    def _transcribe_sync(self, pcm: bytes) -> str:
        wav = pcm_to_wav(pcm)
        data = {"model": self.model}
        if self.language != "auto":
            data["language"] = self.language
        resp = requests.post(
            f"{self.base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            files={"file": ("audio.wav", wav, "audio/wav")},
            data=data,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        try:
            return (resp.json().get("text") or "").strip()
        except ValueError:
            # 某些兼容服务直接返回纯文本
            return resp.text.strip()

    async def recognize(self, pcm: bytes) -> str:
        if not self.base_url or not self.api_key:
            raise RuntimeError("未配置 OpenAI 兼容接口地址或 Key")
        # 异常向上抛给 main.py，由它统一做连续失败计数与熔断退避。
        # 这里吞掉异常会让模型重载期间的每一段都白等一个超时。
        return await asyncio.to_thread(self._transcribe_sync, pcm)
