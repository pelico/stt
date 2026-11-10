"""内存日志缓冲：供 Web 页面查看容器运行日志。"""
import logging
import threading
from collections import deque
from datetime import datetime
from typing import Dict, List


class LogBuffer:
    """固定容量的环形缓冲，按自增 id 支持增量拉取。"""

    def __init__(self, capacity: int = 1000):
        self._items: deque = deque(maxlen=capacity)
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, record: logging.LogRecord) -> None:
        # 带毫秒：识别请求与超时日志往往在同一秒内，秒级时间戳看不出先后
        entry = {
            "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            + ".%03d" % int((record.created % 1) * 1000),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        with self._lock:
            self._seq += 1
            entry["id"] = self._seq
            self._items.append(entry)

    def since(self, after: int = 0, limit: int = 500) -> List[Dict]:
        with self._lock:
            items = [item for item in self._items if item["id"] > after]
        if limit > 0:
            items = items[-limit:]
        return items

    def clear(self) -> int:
        with self._lock:
            self._items.clear()
            return self._seq

    @property
    def last_id(self) -> int:
        with self._lock:
            return self._seq


class BufferHandler(logging.Handler):
    """把日志写入内存缓冲，不影响原有控制台输出。"""

    def __init__(self, buffer: LogBuffer):
        super().__init__()
        self.buffer = buffer
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.add(record)
        except Exception:
            self.handleError(record)
