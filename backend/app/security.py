"""密码哈希、会话令牌、限速等安全原语。

不引入 bcrypt/passlib：标准库 hashlib.scrypt 已经足够，且部署零负担。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time

_SCRYPT_N = 2 ** 14  # 16384，约 40ms/次，兼顾安全与登录体验
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32


def hash_password(password: str) -> str:
    """返回 scrypt$n$r$p$salt_b64$hash_b64 格式字符串。"""
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                        r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = (stored or "").split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64),
                            n=int(n), r=int(r), p=int(p), dklen=_DKLEN)
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:  # noqa: BLE001
        return False


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    """会话令牌在库里只存哈希，泄库也无法直接冒用。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mask_email(email: str) -> str:
    email = (email or "").strip()
    if "@" not in email:
        return email[:2] + "***" if email else ""
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        return name[:1] + "***@" + domain
    return name[:2] + "***@" + domain


# ------------------------------------------------------------------ 简易限速
class RateLimiter:
    """按 key 的滑动窗口限速，进程内即可（单机部署够用）。"""

    def __init__(self, limit: int, window: float = 60.0):
        self.limit = limit
        self.window = window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, cost: int = 1) -> bool:
        """返回 True 表示放行。"""
        now = time.time()
        with self._lock:
            arr = self._hits.get(key)
            if arr is None:
                arr = []
                self._hits[key] = arr
            cutoff = now - self.window
            if len(arr) > 4 * self.limit:  # 防内存膨胀
                arr[:] = [t for t in arr if t > cutoff]
            while arr and arr[0] <= cutoff:
                arr.pop(0)
            if len(arr) + cost > self.limit:
                return False
            arr.extend([now] * cost)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


login_limiter = RateLimiter(limit=8, window=300)      # 5 分钟内最多 8 次登录尝试
register_limiter = RateLimiter(limit=5, window=3600)  # 每小时最多注册 5 个
api_limiter = RateLimiter(limit=60, window=60)
# 验证码出图有 CPU 开销，单独限一道，正常人刷不到这个量
captcha_limiter = RateLimiter(limit=40, window=300)
# 发信按 IP 限速，防止被人拿来刷爆邮箱配额
email_limiter = RateLimiter(limit=20, window=3600)
# 发短信按 IP 限速。短信是真金白银，比邮箱更严；
# 单手机号维度的限速在 smscode.py 里（60 秒冷却 + 每小时 5 条）。
sms_limiter = RateLimiter(limit=12, window=3600)
