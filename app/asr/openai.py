"""OpenAI 兼容的语音转写接口（/audio/transcriptions）。

可指向 OpenAI 官方、任意兼容中转服务，或本地兼容服务。
每次请求都打结构化日志，超时能直接定位到连接/读取阶段与上游状态。
"""
import asyncio
import logging
import time
from urllib.parse import urlsplit

import requests

from app.asr.base import BaseASR, pcm_to_wav
from app.config import settings
from app.diagnostics import describe_exception, metrics

logger = logging.getLogger("stt.asr")


class OpenAISR(BaseASR):
    engine_name = "openai"

    def __init__(self, cfg):
        self.base_url = (cfg.get("openai_base_url") or "").rstrip("/")
        self.api_key = cfg.get("openai_api_key")
        self.model = cfg.get("openai_model") or "whisper-1"
        self.language = cfg.get("asr_language") or "auto"
        self.read_timeout = max(5, int(settings.asr_timeout))
        self.connect_timeout = max(2, int(settings.asr_connect_timeout))
        self.slow_ms = max(1000, int(settings.asr_slow_ms))
        self._endpoint = f"{self.base_url}/audio/transcriptions"
        self._host = urlsplit(self.base_url).netloc or self.base_url
        # 连接复用：识别 worker 是单消费者串行，同一时刻只有一个请求，
        # Session 跨线程共享是安全的。省去每段一次的 TCP+TLS 握手
        # （约 0.3~0.5s）；对端空闲断开时 urllib3 会自动换新连接，
        # 极小概率的陈旧连接失败由队列的重试机制兜底。
        self._session = requests.Session()

    def _transcribe_sync(self, pcm: bytes) -> str:
        req = metrics.next_id()
        wav = pcm_to_wav(pcm)
        duration = len(pcm) / (settings.sample_rate * 2)
        data = {"model": self.model}
        if self.language != "auto":
            data["language"] = self.language

        # 读超时按音频时长自适应：SenseVoice 推理约 1~1.5 倍实时，固定 45s
        # 会被 30s 合并块（推理 30~45s）误杀——超时→重试→服务端单 worker
        # 排队→更慢→再超时，形成活锁。max(固定值, 时长×2+15) 保证正常
        # 推理永不超时；服务端真死时仍由固定值 + 熔断兜底。
        read_timeout = max(self.read_timeout, int(duration * 2) + 15)

        logger.info(
            "[ASR] req#%d → POST %s | host=%s model=%s lang=%s "
            "audio=%.1fs/%dKB wav=%dKB timeout=(connect %ds, read %ds)",
            req, self._endpoint, self._host, self.model, self.language,
            duration, len(pcm) // 1024, len(wav) // 1024,
            self.connect_timeout, read_timeout,
        )

        started = time.monotonic()
        resp = None
        try:
            resp = self._session.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": ("audio.wav", wav, "audio/wav")},
                data=data,
                timeout=(self.connect_timeout, read_timeout),
            )
            latency = time.monotonic() - started
            elapsed = getattr(resp, "elapsed", None)
            ttfb = elapsed.total_seconds() if elapsed else 0.0
            resp.raise_for_status()
            try:
                text = (resp.json().get("text") or "").strip()
            except ValueError:
                # 某些兼容服务直接返回纯文本
                text = resp.text.strip()

            slow = latency * 1000 > self.slow_ms
            metrics.record(ok=True, latency_s=latency, slow=slow, url=self._host)
            logger.info(
                "[ASR] req#%d ✓ %.2fs (ttfb %.2fs, http %d, resp %dB) text=%d字: %s",
                req, latency, ttfb, getattr(resp, "status_code", 0),
                len(getattr(resp, "content", b"") or b""),
                len(text), text[:80].replace("\n", " "),
            )
            if slow:
                logger.warning(
                    "[ASR] req#%d 慢请求 %.2fs > %.1fs：识别服务端处理过慢，"
                    "查 GCP 的 CPU/swap/模型加载与 Cloudflare 回源耗时",
                    req, latency, self.slow_ms / 1000,
                )
            return text
        except Exception as e:
            latency = time.monotonic() - started
            info = describe_exception(e, getattr(e, "response", None) or resp)
            metrics.record(
                ok=False,
                latency_s=latency,
                code=info["code"],
                detail=info["detail"],
                hint=info["hint"],
                url=self._host,
            )
            logger.error(
                "[ASR] req#%d ✗ %s after %.2fs | host=%s | %s | 建议: %s",
                req, info["code"], latency, self._host,
                info["detail"], info["hint"],
            )
            # 异常向上抛给 main.py，由它统一做连续失败计数与熔断退避
            raise

    async def recognize(self, pcm: bytes) -> str:
        if not self.base_url or not self.api_key:
            raise RuntimeError("未配置 OpenAI 兼容接口地址或 Key")
        return await asyncio.to_thread(self._transcribe_sync, pcm)
