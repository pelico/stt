"""ASR 调用的结构化诊断：异常归类、耗时统计、统一日志字段。

目标：出现 `Read timed out` 时，日志能直接回答——
请求了谁、超时设多少、等了多久、卡在连接还是读取、服务端返回什么、
当前连续失败了几次、积压多少、下一步该查哪里。
"""
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from itertools import count
from typing import Any, Dict, Optional

logger = logging.getLogger("stt.asr")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def describe_exception(exc: BaseException, response=None) -> Dict[str, str]:
    """把底层异常归一化成 {code, detail, hint}，code 用于统计聚合。"""
    name = type(exc).__name__
    module = type(exc).__module__ or ""
    raw = str(exc)
    status = getattr(response, "status_code", None)

    if status is not None:
        body = (getattr(response, "text", "") or "").strip().replace("\n", " ")[:300]
        if status in (502, 503, 504):
            hint = "反代/上游不可用或网关超时：检查 Caddy 反代目标、sensevoice 进程状态与 GCP 负载"
        elif status in (401, 403):
            hint = "鉴权失败：检查 API Key，以及反代是否透传 Authorization 头"
        elif status == 404:
            hint = "路径不存在：检查 OPENAI_BASE_URL 与反代的 handle_path 前缀"
        elif status == 413:
            hint = "音频过大：检查单段时长与代理的上传体积限制"
        elif 500 <= status < 600:
            hint = "服务端 5xx：查 GCP 上 journalctl -u sensevoice"
        else:
            hint = "服务端返回非 2xx：看下方响应体"
        return {
            "code": "http_%d" % status,
            "detail": "HTTP %d | %s | body=%r" % (status, raw[:120], body),
            "hint": hint,
        }

    if "ReadTimeout" in name:
        code = "read_timeout"
        hint = ("连接已建立但服务端在读取超时内没返回：多半是推理慢/被 swap 拖住，"
                "检查 GCP 的 CPU、vmstat si/so、MemoryMax 与模型加载状态；"
                "若本机直连快而经域名慢，则查 Cloudflare 回源")
    elif "ConnectTimeout" in name:
        code = "connect_timeout"
        hint = "TCP/TLS 握手超时：查防火墙、Cloudflare 回源、Caddy 是否存活、域名解析"
    elif "ProxyError" in name:
        code = "proxy_error"
        hint = "代理链路异常：查容器网络与代理设置"
    elif "SSLError" in name:
        code = "ssl_error"
        hint = "TLS 证书校验失败：查域名证书与 Cloudflare Full(strict) 配置"
    elif "ConnectionError" in name:
        code = "connection_error"
        hint = "连接被重置/拒绝或 DNS 失败：查上游是否监听、端口与反代目标是否正确"
    elif "HTTPError" in name:
        code = "http_error"
        hint = "服务端返回非 2xx：查反代与上游日志"
    elif "Timeout" in name:
        code = "timeout"
        hint = "请求超时：区分连接阶段与读取阶段，必要时分别调大两个超时"
    elif "JSONDecodeError" in name or "ValueError" in name:
        code = "bad_response"
        hint = "响应体不是预期 JSON/文本：可能返回了错误页，检查反代路径"
    else:
        code = "other"
        hint = "未归类异常：结合下方原始异常与上游日志排查"

    return {
        "code": code,
        "detail": "%s.%s: %s" % (module, name, raw[:400]),
        "hint": hint,
    }


class ASRMetrics:
    """最近 N 次识别调用的滑动统计，供 /api/health 与前端摘要使用。"""

    def __init__(self, window: int = 50):
        self._lock = threading.Lock()
        self._latencies = deque(maxlen=window)
        self._ids = count(1)
        self._calls = 0
        self._ok = 0
        self._failed = 0
        self._slow = 0
        self._by_code: Dict[str, int] = {}
        self._last_error: Optional[Dict[str, Any]] = None
        self._last_ok_at: Optional[str] = None
        self._last_latency_ms: Optional[float] = None
        self._consecutive_failures = 0

    def next_id(self) -> int:
        return next(self._ids)

    def record(
        self,
        ok: bool,
        latency_s: float,
        code: str = "",
        detail: str = "",
        hint: str = "",
        slow: bool = False,
        url: str = "",
    ) -> None:
        ms = round(latency_s * 1000, 1)
        with self._lock:
            self._calls += 1
            self._latencies.append(ms)
            self._last_latency_ms = ms
            if slow:
                self._slow += 1
            if ok:
                self._ok += 1
                self._consecutive_failures = 0
                self._last_ok_at = _now()
            else:
                self._failed += 1
                self._consecutive_failures += 1
                key = code or "other"
                self._by_code[key] = self._by_code.get(key, 0) + 1
                self._last_error = {
                    "at": _now(),
                    "code": key,
                    "detail": detail,
                    "hint": hint,
                    "latency_ms": ms,
                    "url": url,
                }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            lat = sorted(self._latencies)
            n = len(lat)
            return {
                "calls": self._calls,
                "ok": self._ok,
                "failed": self._failed,
                "success_rate": round(self._ok / self._calls * 100, 1) if self._calls else None,
                "consecutive_failures": self._consecutive_failures,
                "slow_calls": self._slow,
                "last_latency_ms": self._last_latency_ms,
                "avg_latency_ms": round(sum(lat) / n, 1) if n else None,
                "p95_latency_ms": lat[min(n - 1, int(n * 0.95))] if n else None,
                "max_latency_ms": lat[-1] if n else None,
                "error_counts": dict(self._by_code),
                "last_error": dict(self._last_error) if self._last_error else None,
                "last_success_at": self._last_ok_at,
            }


metrics = ASRMetrics()
