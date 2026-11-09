"""ASR 引擎工厂：按运行时配置选择并缓存实例。"""
import logging
from typing import Dict, Tuple

from app.asr.base import BaseASR, pcm_to_wav
from app.asr.aliyun import AliyunASR
from app.asr.tencent import TencentASR
from app.asr.openai import OpenAISR

logger = logging.getLogger(__name__)

_ENGINES = {
    "aliyun": AliyunASR,
    "tencent": TencentASR,
    "openai": OpenAISR,
}

# engine -> (签名, 实例)
_cache: Dict[str, Tuple[tuple, BaseASR]] = {}


def _signature(engine: str, cfg) -> tuple:
    return (
        engine,
        cfg.get("asr_model"),
        cfg.get("asr_language"),
        cfg.get("dashscope_api_key"),
        cfg.get("tencent_secret_id"),
        cfg.get("tencent_secret_key"),
        cfg.get("tencent_region"),
        cfg.get("openai_base_url"),
        cfg.get("openai_api_key"),
        cfg.get("openai_model"),
    )


def get_asr(cfg) -> BaseASR:
    """根据配置返回 ASR 实例，配置变化时自动重建。"""
    engine = cfg.get("asr_engine") or "aliyun"
    cls = _ENGINES.get(engine)
    if cls is None:
        logger.warning("未知 ASR 引擎 %s，回退到 aliyun", engine)
        engine = "aliyun"
        cls = AliyunASR

    sig = _signature(engine, cfg)
    cached = _cache.get(engine)
    if cached and cached[0] == sig:
        return cached[1]

    instance = cls(cfg)
    _cache[engine] = (sig, instance)
    logger.info("初始化 ASR 引擎: %s", engine)
    return instance


__all__ = ["get_asr", "BaseASR", "pcm_to_wav"]
