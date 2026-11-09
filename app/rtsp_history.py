"""RTSP 地址历史记录（最多 10 条，最近的在前）。"""
import json
import logging
import os
import threading
from typing import List

from app.config import settings

logger = logging.getLogger("stt.history")
MAX_ITEMS = 10
_lock = threading.Lock()


def _path() -> str:
    base = os.path.dirname(settings.settings_path) or "."
    return os.path.join(base, "rtsp_history.json")


def _load() -> List[str]:
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(item) for item in data if isinstance(item, str) and item]
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("读取 RTSP 历史失败: %s", e)
    return []


def _save(items: List[str]) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def list_history() -> List[str]:
    with _lock:
        return _load()


def add_history(url: str) -> List[str]:
    url = (url or "").strip()
    if not url:
        return list_history()
    with _lock:
        items = [item for item in _load() if item != url]
        items.insert(0, url)
        items = items[:MAX_ITEMS]
        _save(items)
        return items


def remove_history(url: str) -> List[str]:
    with _lock:
        items = [item for item in _load() if item != url]
        _save(items)
        return items
