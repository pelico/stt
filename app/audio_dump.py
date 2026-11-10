"""把被过滤掉的语音片段存成 WAV，便于回听判断是噪声还是误杀。

默认关闭（SAVE_REJECTED_AUDIO=true 开启），文件写到 /data/audio/rejected/，
按数量自动清理，避免在玩客云的 SD 卡上无限堆积。
"""
import io
import logging
import os
import re
import wave
from datetime import datetime
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

_SAFE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_-]+")
_SAMPLE_RATE = 16000


def _to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def _prune(folder: str, keep: int):
    try:
        files = sorted(
            (
                os.path.join(folder, name)
                for name in os.listdir(folder)
                if name.endswith(".wav")
            ),
            key=os.path.getmtime,
        )
        extra = files[:-keep] if keep > 0 else files
        for path in extra:
            os.remove(path)
    except Exception as e:
        logger.warning("清理被过滤音频失败: %s", e)


def dump_rejected(pcm: bytes, reason: str = "rejected") -> Optional[str]:
    """同步写一个 WAV，返回文件名；调用方应放到线程里执行。"""
    if not pcm:
        return None
    folder = os.path.join(settings.audio_dir, "rejected")
    os.makedirs(folder, exist_ok=True)
    tag = _SAFE.sub("-", reason)[:24].strip("-") or "rejected"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    name = f"{stamp}_{tag}.wav"
    with open(os.path.join(folder, name), "wb") as f:
        f.write(_to_wav(pcm))
    _prune(folder, max(1, int(settings.rejected_audio_max)))
    return name
