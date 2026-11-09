"""运行时配置：环境变量作为默认值，Web 设置持久化到 JSON，支持热更新。"""
import json
import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# 可热更新的字段 -> 类型
FIELD_TYPES: Dict[str, type] = {
    "rtsp_url": str,
    "asr_engine": str,
    "asr_model": str,
    "dashscope_api_key": str,
    "tencent_secret_id": str,
    "tencent_secret_key": str,
    "tencent_region": str,
    "openai_base_url": str,
    "openai_api_key": str,
    "openai_model": str,
    "vad_mode": int,
    "vad_frame_ms": int,
    "vad_silence_frames": int,
    "vad_min_speech_frames": int,
}

# 需要打码的字段
SECRET_FIELDS = {
    "dashscope_api_key",
    "tencent_secret_id",
    "tencent_secret_key",
    "openai_api_key",
}

DEFAULTS: Dict[str, Any] = {
    "rtsp_url": settings.rtsp_url,
    "asr_engine": settings.asr_engine,
    "asr_model": settings.asr_model,
    "dashscope_api_key": settings.dashscope_api_key,
    "tencent_secret_id": settings.tencent_secret_id,
    "tencent_secret_key": settings.tencent_secret_key,
    "tencent_region": settings.tencent_region,
    "openai_base_url": settings.openai_base_url,
    "openai_api_key": settings.openai_api_key,
    "openai_model": settings.openai_model,
    "vad_mode": settings.vad_mode,
    "vad_frame_ms": settings.vad_frame_ms,
    "vad_silence_frames": settings.vad_silence_frames,
    "vad_min_speech_frames": settings.vad_min_speech_frames,
}

ENGINES = ("aliyun", "tencent", "openai")
ALLOWED_FRAME_MS = (10, 20, 30)


def _mask(value: str) -> str:
    """敏感值打码，只保留首尾少量字符。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}****{value[-4:]}"


def _validate(key: str, value: Any) -> Any:
    """校验并归一化单个字段。"""
    expected = FIELD_TYPES[key]

    if expected is int:
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须是整数")

    if key == "vad_mode" and value not in (0, 1, 2, 3):
        raise ValueError("vad_mode 必须是 0-3")
    if key == "vad_frame_ms" and value not in ALLOWED_FRAME_MS:
        raise ValueError("vad_frame_ms 只能是 10/20/30")
    if key == "vad_silence_frames" and not (1 <= value <= 200):
        raise ValueError("vad_silence_frames 必须在 1-200")
    if key == "vad_min_speech_frames" and not (1 <= value <= 100):
        raise ValueError("vad_min_speech_frames 必须在 1-100")
    if key == "asr_engine" and value not in ENGINES:
        raise ValueError(f"asr_engine 必须是 {'/'.join(ENGINES)}")

    if expected is str:
        if value is None:
            value = ""
        value = str(value).strip()

    return value


class RuntimeConfig:
    """线程安全的运行时配置。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._values: Dict[str, Any] = dict(DEFAULTS)
        self._listeners: List[Callable[[List[str]], None]] = []
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key, value in data.items():
                if key in FIELD_TYPES:
                    try:
                        self._values[key] = _validate(key, value)
                    except ValueError as e:
                        logger.warning("忽略非法配置 %s: %s", key, e)
            logger.info("已加载运行时配置: %s", self.path)
        except Exception as e:
            logger.error("读取配置文件失败 %s: %s", self.path, e)

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._values, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.error("保存配置文件失败 %s: %s", self.path, e)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._values.get(key, default)

    def subscribe(self, callback: Callable[[List[str]], None]):
        self._listeners.append(callback)

    def snapshot(self, mask: bool = True) -> Dict[str, Any]:
        """返回全部配置；mask=True 时敏感字段打码。"""
        with self._lock:
            data = dict(self._values)
        if mask:
            for key in SECRET_FIELDS:
                data[key] = _mask(data.get(key, ""))
                data[f"{key}_set"] = bool(self._values.get(key))
        return data

    def update(self, patch: Dict[str, Any]) -> List[str]:
        """增量更新，返回实际变化的字段列表。

        敏感字段传空字符串或打码值时表示不修改。
        """
        changed: List[str] = []
        with self._lock:
            for key, raw in patch.items():
                if key not in FIELD_TYPES:
                    continue
                value = _validate(key, raw)
                if key in SECRET_FIELDS and (value == "" or "****" in value):
                    continue
                if self._values.get(key) != value:
                    self._values[key] = value
                    changed.append(key)
            if changed:
                self._save()
        if changed:
            logger.info("配置已更新: %s", ", ".join(changed))
            for callback in list(self._listeners):
                try:
                    callback(changed)
                except Exception as e:
                    logger.error("配置变更回调失败: %s", e)
        return changed


_runtime: Optional[RuntimeConfig] = None


def get_runtime() -> RuntimeConfig:
    global _runtime
    if _runtime is None:
        _runtime = RuntimeConfig(settings.settings_path)
    return _runtime
