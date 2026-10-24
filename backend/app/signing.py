"""短时效媒体签名链接。

浏览器 <video> 标签没法带 Authorization 头，桌面端又用 Bearer，
所以成品地址统一走 ?t=<签名>：HMAC(asset_id|exp) ，默认 6 小时有效。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from .config import CFG

_TTL = 6 * 3600


def _sig(asset_id, exp: int) -> str:
    raw = "%s|%s" % (asset_id, exp)
    mac = hmac.new(CFG.secret_key.encode(), raw.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")[:32]


def ticket(asset_id) -> str:
    exp = int(time.time()) + _TTL
    return "%d.%s" % (exp, _sig(asset_id, exp))


def verify(asset_id, t: str) -> bool:
    if not t or "." not in t:
        return False
    exp_s, sig = t.split(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(sig, _sig(asset_id, exp))


def media_url(asset_id) -> str:
    return "/api/media/%s?t=%s" % (asset_id, ticket(asset_id))
