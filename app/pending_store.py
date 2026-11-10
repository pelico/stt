"""待识别语音段的磁盘持久化队列（「完整优先」模式）。

音频由采集端（rtsp-stt 容器）产生，队列也落在采集端的 ./data 卷里，
不放到 ASR 服务端：服务端只负责推理，往本就吃紧的 1G 内存机器上再堆
几百条 WAV 只会让 swap 更严重；而且音频没发过去之前服务端也无从排队。

用独立 SQLite 文件（WAL）而不是散落的 WAV 文件：入队、取最旧、出队、
按条数/字节裁剪都是原子操作，容器重启后未处理的片段也不会丢。
"""
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

from app.config import settings


def utc_iso_naive() -> str:
    """采集时刻，格式与 database.Transcript.timestamp 一致（naive UTC ISO）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def parse_iso_naive(value: Optional[str]) -> Optional[datetime]:
    """把采集时刻还原成 naive UTC datetime；非法值返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


class PendingStore:
    """按 id（即采集顺序）FIFO 消费的待识别队列。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(
            os.path.dirname(settings.db_path) or ".", "pending.db"
        )
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._run(
            lambda conn: (
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS pending (
                           id         INTEGER PRIMARY KEY AUTOINCREMENT,
                           wall_iso   TEXT    NOT NULL,
                           created_at REAL    NOT NULL,
                           pcm        BLOB    NOT NULL,
                           attempts   INTEGER NOT NULL DEFAULT 0,
                           size       INTEGER NOT NULL
                       )"""
                ),
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pending_id ON pending(id)"
                ),
            )
        )

    def _run(self, fn):
        """每次操作独立连接（sqlite3 连接不跨线程），串行化保证裁剪原子。"""
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=15)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                result = fn(conn)
                conn.commit()
                return result
            finally:
                conn.close()

    def put(
        self,
        pcm: bytes,
        wall_iso: str,
        max_items: int,
        max_mb: int,
    ) -> Tuple[int, int]:
        """入队并裁剪。返回 (当前积压条数, 因超限被丢弃的条数)。"""
        def _do(conn):
            conn.execute(
                "INSERT INTO pending (wall_iso, created_at, pcm, attempts, size) "
                "VALUES (?, ?, ?, 0, ?)",
                (wall_iso, time.time(), pcm, len(pcm)),
            )
            dropped = 0
            if max_items > 0:
                cur = conn.execute(
                    "DELETE FROM pending WHERE id IN ("
                    "SELECT id FROM pending ORDER BY id DESC LIMIT -1 OFFSET ?)",
                    (max_items,),
                )
                dropped += cur.rowcount or 0
            if max_mb > 0:
                limit = max_mb * 1024 * 1024
                rows = conn.execute(
                    "SELECT id, size FROM pending ORDER BY id"
                ).fetchall()
                total = sum(r[1] for r in rows)
                doomed = []
                for row_id, size in rows:
                    if total <= limit:
                        break
                    doomed.append((row_id,))
                    total -= size
                if doomed:
                    conn.executemany("DELETE FROM pending WHERE id = ?", doomed)
                    dropped += len(doomed)
            count = conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
            return count, dropped
        return self._run(_do)

    def peek(self) -> Optional[Tuple[int, bytes, str, int, float]]:
        """取最旧一条但不删除；处理成功后才由 delete() 出队。

        第 5 项是该段已排队秒数，供日志显示「等待多久才轮到识别」。
        """
        return self._run(
            lambda conn: conn.execute(
                "SELECT id, pcm, wall_iso, attempts, ? - created_at FROM pending "
                "ORDER BY id LIMIT 1",
                (time.time(),),
            ).fetchone()
        )

    def peek_batch(self, max_items: int, max_bytes: int) -> list:
        """按 FIFO 取最旧的一批段，供消费端合并成一次识别请求。

        只按条数与总字节数截断（单条超长也会整条返回）；
        「相邻间隔是否够近」由调用方结合 created_at 与段时长判断。
        返回 [(id, pcm, wall_iso, attempts, created_at, size), ...]。
        """
        def _do(conn):
            rows = conn.execute(
                "SELECT id, pcm, wall_iso, attempts, created_at, size "
                "FROM pending ORDER BY id LIMIT ?",
                (max(1, int(max_items)),),
            ).fetchall()
            out, total = [], 0
            for row in rows:
                if out and total + row[5] > max_bytes:
                    break
                out.append(row)
                total += row[5]
            return out
        return self._run(_do)

    def delete_many(self, ids) -> None:
        """批量出队：合并识别成功后一次性删除。"""
        ids = list(ids)
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        self._run(
            lambda conn: conn.execute(
                f"DELETE FROM pending WHERE id IN ({placeholders})", ids
            )
        )

    def bump_many(self, ids, max_attempts: int) -> int:
        """识别失败：一批各计数 +1，达到上限的丢弃。返回丢弃条数。"""
        def _do(conn):
            removed = 0
            for row_id in ids:
                row = conn.execute(
                    "SELECT attempts FROM pending WHERE id = ?", (row_id,)
                ).fetchone()
                if row is None:
                    continue
                attempts = row[0] + 1
                if max_attempts > 0 and attempts >= max_attempts:
                    conn.execute("DELETE FROM pending WHERE id = ?", (row_id,))
                    removed += 1
                else:
                    conn.execute(
                        "UPDATE pending SET attempts = ? WHERE id = ?",
                        (attempts, row_id),
                    )
            return removed
        return self._run(_do)

    def delete(self, row_id: int):
        self._run(lambda conn: conn.execute("DELETE FROM pending WHERE id = ?", (row_id,)))

    def bump(self, row_id: int, max_attempts: int) -> bool:
        """识别失败：重试计数 +1；达到上限则丢弃，返回是否因超限丢弃。"""
        def _do(conn):
            row = conn.execute(
                "SELECT attempts FROM pending WHERE id = ?", (row_id,)
            ).fetchone()
            if row is None:
                return False
            attempts = row[0] + 1
            if max_attempts > 0 and attempts >= max_attempts:
                conn.execute("DELETE FROM pending WHERE id = ?", (row_id,))
                return True
            conn.execute(
                "UPDATE pending SET attempts = ? WHERE id = ?", (attempts, row_id)
            )
            return False
        return self._run(_do)

    def count(self) -> int:
        return int(
            self._run(
                lambda conn: conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
            )
        )

    def stats(self) -> Tuple[int, int, float]:
        """返回 (积压条数, 待识别音频总字节数, 处理滞后秒数)。

        滞后 = 当前时间 - 最老一条的采集时刻，即「现在正在补多久之前的话」；
        队列为空时为 0。
        """
        def _do(conn):
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0), "
                "COALESCE(MIN(created_at), 0) FROM pending"
            ).fetchone()
            count, total, oldest = int(row[0]), int(row[1]), float(row[2])
            lag = max(0.0, time.time() - oldest) if count and oldest else 0.0
            return count, total, lag
        return self._run(_do)

    def maybe_vacuum(self, threshold_mb: int = 64) -> bool:
        """队列清空后回收磁盘：SQLite DELETE 不缩小文件，积压消化完
        pending.db 仍占着峰值空间。空表且文件超过阈值时执行 VACUUM。
        返回是否执行了回收。"""
        try:
            if self.count() != 0:
                return False
            if os.path.getsize(self.path) < threshold_mb * 1024 * 1024:
                return False
        except OSError:
            return False
        self._run(lambda conn: conn.execute("VACUUM"))
        return True
