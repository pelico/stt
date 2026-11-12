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
        # 慢请求动态预测：预计耗时 = 时长 × 实时率 + 固定开销；短音频仍受
        # slow_ms 固定基线保护。实时率用成功请求实测值自学习（EMA 修正），
        # 服务端变快/变慢阈值自动跟随，不用手调
        self._slow_rt_factor = max(0.1, float(settings.asr_slow_rt_factor))
        self._slow_overhead = max(0.0, float(settings.asr_slow_overhead_sec))
        self._slow_margin = max(1.0, float(settings.asr_slow_margin))
        self._learned_rt = self._slow_rt_factor  # 实测实时率的 EMA，初始=配置值
        self._endpoint = f"{self.base_url}/audio/transcriptions"
        self._host = urlsplit(self.base_url).netloc or self.base_url
        # 连接复用：识别 worker 是单消费者串行，同一时刻只有一个请求，
        # Session 跨线程共享是安全的。省去每段一次的 TCP+TLS 握手
        # （约 0.3~0.5s）；对端空闲断开时 urllib3 会自动换新连接，
        # 极小概率的陈旧连接失败由队列的重试机制兜底。
        self._session = requests.Session()

    def _predict(self, duration: float) -> float:
        """预计总耗时 = 时长 × 实时率 + 固定开销。

        取「配置模型」与「实测学习模型」的较大值：学习结果只抬高预测
        不降低（服务端偶尔的快请求不会把阈值拉得过敏感）。慢请求告警
        与读超时共用本预测，保证「先告警、后超时」的次序不倒挂。
        """
        predict_cfg = duration * self._slow_rt_factor + self._slow_overhead
        predict_learned = duration * self._learned_rt + self._slow_overhead
        return max(predict_cfg, predict_learned)

    def _transcribe_sync(self, pcm: bytes) -> str:
        req = metrics.next_id()
        wav = pcm_to_wav(pcm)
        duration = len(pcm) / (settings.sample_rate * 2)
        data = {"model": self.model}
        if self.language != "auto":
            data["language"] = self.language

        # 读超时同样走动态预测：SenseVoice 推理约 1~1.5 倍实时，固定 45s
        # 会被 30s 合并块（推理 30~45s）误杀——超时→重试→服务端单 worker
        # 排队→更慢→再超时，形成活锁。现在超时 = 预测耗时 × 容差 + 10s 余量，
        # 与慢请求告警共用同一个预测器：告警阈值之下仍能拿到结果（慢但成功），
        # 超过告警阈值还有 10s 宽限再放弃；服务端整体变慢时实测学习的实时率
        # 抬高预测，超时跟着放宽，不会退化成固定公式。服务端真死时仍由
        # 固定下限 + 熔断兜底。
        read_timeout = max(
            self.read_timeout,
            int(self._predict(duration) * self._slow_margin) + 10,
        )

        # 慢请求告警阈值：短音频不高于固定基线 slow_ms
        predict_s = self._predict(duration)
        slow_threshold_s = max(
            self.slow_ms / 1000.0, predict_s * self._slow_margin
        )

        ctx = getattr(metrics, "queue_ctx", "")
        logger.info(
            "[ASR] req#%d ASR请求 %s%s | 音频 %.1fs/%dKB | 预测 %.1fs(rt=%.2f)",
            req, f"{ctx} | " if ctx else "", self._host, duration,
            len(pcm) // 1024, predict_s, self._learned_rt,
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
            resp.raise_for_status()
            try:
                text = (resp.json().get("text") or "").strip()
            except ValueError:
                # 某些兼容服务直接返回纯文本
                text = resp.text.strip()

            slow = latency > slow_threshold_s
            # 自学习：用实测耗时修正实时率（扣掉固定开销后除以时长），
            # EMA 平滑防单次抖动；限幅防止异常值把阈值带偏
            if duration >= 1.0:
                rt = (latency - self._slow_overhead) / duration
                rt = min(max(rt, 0.1), 5.0)
                self._learned_rt = 0.7 * self._learned_rt + 0.3 * rt
            metrics.record(ok=True, latency_s=latency, slow=slow, url=self._host)
            logger.info(
                "[ASR] req#%d ASR结果 %s %.2fs/预测%.1fs | %d字: %s",
                req, "⚠" if slow else "✓", latency, predict_s,
                len(text), text[:80].replace("\n", " "),
            )
            if slow:
                logger.warning(
                    "[ASR] req#%d 慢请求 %.2fs > 预测 %.1fs×%.1f "
                    "(音频 %.1fs, 实时率 %.2f)：识别服务端处理过慢，"
                    "查 GCP 的 CPU/swap/模型加载与 Cloudflare 回源耗时",
                    req, latency, predict_s, self._slow_margin,
                    duration, self._learned_rt,
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
                "[ASR] req#%d ASR失败 %s after %.2fs | host=%s | %s | 建议: %s",
                req, info["code"], latency, self._host,
                info["detail"], info["hint"],
            )
            # 异常向上抛给 main.py，由它统一做连续失败计数与熔断退避
            raise

    async def recognize(self, pcm: bytes) -> str:
        if not self.base_url or not self.api_key:
            raise RuntimeError("未配置 OpenAI 兼容接口地址或 Key")
        return await asyncio.to_thread(self._transcribe_sync, pcm)
