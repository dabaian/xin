"""支付宝当面付（扫码充值）客户端。

流程：
  1. 服务端生成订单号 + 金额（**金额只由服务端决定**，绝不信任前端）
  2. 调 alipay.trade.precreate 拿到 qr_code 字符串
  3. 前端把 qr_code 渲染成二维码，用户用支付宝 App 扫码付款
  4. 支付宝异步通知 notify_url → 验签 → 幂等入账（唯一约束兜底）
  5. 前端轮询订单状态；同时后台用 alipay.trade.query 主动查单兜底
     （通知丢失也能自动到账）

签名算法 RSA2 = SHA256withRSA；字符串拼接规则见 _sign_content()。
"""
from __future__ import annotations

import base64
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from ..config import CFG

log = logging.getLogger("studio.alipay")

GATEWAY = "https://openapi.alipay.com/gateway.do"


class AlipayError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------ 密钥
def _wrap_pem(raw: str, header: str) -> str:
    raw = (raw or "").strip()
    if "BEGIN" in raw:
        return raw
    body = "".join(raw.split())
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN %s-----\n%s\n-----END %s-----" % (header, "\n".join(lines), header)


def _private_key():
    pem = _wrap_pem(CFG.alipay_private_key, "RSA PRIVATE KEY")
    try:
        return serialization.load_pem_private_key(pem.encode(), password=None)
    except ValueError:
        pem = _wrap_pem(CFG.alipay_private_key, "PRIVATE KEY")
        return serialization.load_pem_private_key(pem.encode(), password=None)


def _public_key():
    pem = _wrap_pem(CFG.alipay_public_key, "PUBLIC KEY")
    return serialization.load_pem_public_key(pem.encode())


def _sign(content: str) -> str:
    key = _private_key()
    sig = key.sign(content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


def _verify(content: str, signature: str) -> bool:
    try:
        key = _public_key()
        key.verify(base64.b64decode(signature), content.encode("utf-8"),
                   padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ 拼串
def _sign_content(params: dict) -> str:
    """按 key 升序，剔除空值和 sign，value 不编码，用 & 连接。"""
    items = []
    for k in sorted(params.keys()):
        if k in ("sign", "sign_type"):
            continue
        v = params[k]
        if v is None or v == "":
            continue
        items.append("%s=%s" % (k, v))
    return "&".join(items)


# ------------------------------------------------------------------ 调用
def _request(method: str, biz: dict, extra: dict | None = None,
             timeout: float = 20.0) -> dict:
    if not CFG.alipay_ready:
        raise AlipayError("NOT_CONFIGURED",
                          "支付宝未配置：请在 .env 填写 ALIPAY_APP_ID / 应用私钥 / 支付宝公钥")
    params = {
        "app_id": CFG.alipay_app_id,
        "method": method,
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "biz_content": json.dumps(biz, ensure_ascii=False, separators=(",", ":")),
    }
    if extra:
        params.update({k: v for k, v in extra.items() if v})
    params["sign"] = _sign(_sign_content(params))
    body = urllib.parse.urlencode(params, quote_via=urllib.parse.quote).encode("utf-8")

    req = urllib.request.Request(
        CFG.alipay_gateway or GATEWAY, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl.create_default_context()) as resp:
            raw = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        raise AlipayError("HTTP_%s" % exc.code,
                          "支付宝网关返回 %s" % exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AlipayError("NETWORK", "连不上支付宝网关：%s" % exc) from exc

    try:
        pack = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AlipayError("BAD_RESPONSE", "支付宝返回非 JSON：%s" % raw[:200]) from exc

    node = method.replace(".", "_") + "_response"
    data = pack.get(node) or {}
    if not data:
        # 网关级错误
        err = pack.get("error_response") or {}
        raise AlipayError(err.get("code", "GATEWAY"), err.get("sub_msg") or err.get("msg") or raw[:200])
    code = str(data.get("code") or "")
    if code != "10000":
        raise AlipayError(code, data.get("sub_msg") or data.get("msg") or "支付宝返回失败")
    return data


# ------------------------------------------------------------------ 业务
def precreate(order_no: str, amount_minor: int, subject: str,
              timeout_express: str = "") -> str:
    """当面付预下单，返回 qr_code 字符串（前端渲染成二维码）。"""
    yuan = "%.2f" % (amount_minor / 100.0)
    biz = {
        "out_trade_no": order_no,
        "total_amount": yuan,
        "subject": subject[:120],
    }
    texp = timeout_express or CFG.alipay_timeout or "30m"
    if texp:
        biz["timeout_express"] = texp
    data = _request("alipay.trade.precreate", biz,
                    extra={"notify_url": CFG.alipay_notify_url or None})
    qr = data.get("qr_code") or ""
    if not qr:
        raise AlipayError("NO_QRCODE", "支付宝没有返回二维码内容")
    return qr


def query(order_no: str) -> dict:
    """主动查单。返回 {trade_status, trade_no, buyer_id, total_amount}"""
    data = _request("alipay.trade.query", {"out_trade_no": order_no})
    return {
        "trade_status": str(data.get("trade_status") or ""),
        "trade_no": str(data.get("trade_no") or ""),
        "buyer_id": str(data.get("buyer_user_id") or data.get("buyer_logon_id") or ""),
        "total_amount": str(data.get("total_amount") or ""),
        "raw": data,
    }


def close(order_no: str) -> bool:
    try:
        _request("alipay.trade.close", {"out_trade_no": order_no})
        return True
    except AlipayError:
        return False


# ------------------------------------------------------------------ 通知
def verify_notify(form: dict) -> bool:
    """异步通知验签。form 是支付宝 POST 过来的表单（已 url-decode）。"""
    sign = form.get("sign") or ""
    if not sign:
        return False
    # 支付宝规则：去掉 sign、sign_type 后按 key 升序拼串，value 用原文（已 url_decode）
    content = "&".join(
        "%s=%s" % (k, form[k]) for k in sorted(form.keys())
        if k not in ("sign", "sign_type") and form[k] not in (None, ""))
    return _verify(content, sign)


def verify_return(params: dict) -> bool:
    """同步跳转验签（可选使用）。"""
    sign = params.get("sign") or ""
    if not sign:
        return False
    content = "&".join(
        "%s=%s" % (k, params[k]) for k in sorted(params.keys())
        if k not in ("sign", "sign_type") and params[k] not in (None, ""))
    return _verify(content, sign)


def selfcheck() -> dict:
    """配置自检：能否加载密钥、密钥是否配对。"""
    out = {"configured": CFG.alipay_ready, "app_id": CFG.alipay_app_id}
    if not CFG.alipay_ready:
        out["error"] = "缺少 APP_ID / 应用私钥 / 支付宝公钥"
        return out
    try:
        content = "studio-selfcheck"
        sig = _sign(content)
        out["sign_ok"] = bool(sig)
        # 用应用公钥无法验自己的签名（那是支付宝公钥），这里只验证能正常签名
        out["private_key_ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["private_key_ok"] = False
        out["error"] = "私钥无法加载：%s" % exc
    try:
        _public_key()
        out["public_key_ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["public_key_ok"] = False
        out["error"] = "支付宝公钥无法加载：%s" % exc
    return out
