"""登录会话：口令校验 + HMAC 签名 Cookie。

设计取舍：
- 不引依赖：不搞用户表/JWT 库，单口令服务用 HMAC 时间戳令牌足够
- 密钥从口令派生（sha256）：改口令即全体会话失效，重启不丢
- 令牌自带过期（30 天），泄露窗口有限
- 口令比较走 hmac.compare_digest 防时序侧信道
"""
import hashlib
import hmac
import time

COOKIE_NAME = "stt_session"
SESSION_TTL_SEC = 30 * 24 * 3600  # 30 天


def _secret(password: str) -> bytes:
    return hashlib.sha256(b"stt-session-v1:" + password.encode("utf-8")).digest()


def password_matches(supplied: str, password: str) -> bool:
    return hmac.compare_digest((supplied or "").encode("utf-8"), password.encode("utf-8"))


def make_token(password: str, now: float | None = None) -> str:
    """格式: <过期时间戳>.<HMAC(过期时间戳)>"""
    exp = int((now if now is not None else time.time()) + SESSION_TTL_SEC)
    sig = hmac.new(_secret(password), str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_token(token: str, password: str, now: float | None = None) -> bool:
    """校验签名与有效期；任何格式问题都算无效。"""
    try:
        exp_str, sig = (token or "").split(".", 1)
        exp = int(exp_str)
    except (ValueError, AttributeError):
        return False
    expect = hmac.new(_secret(password), exp_str.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return False
    return exp >= (now if now is not None else time.time())
