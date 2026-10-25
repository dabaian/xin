"""极验 GeeTest v4 行为验证（替代原来的自绘图形验证码）。

流程（官方 v4 标准流程）：

    前端  加载 https://static.geetest.com/v4/gt4.js
          → initGeetest4({captchaId}) → showCaptcha()
          → 用户通过后拿到 4 个参数：lot_number / captcha_output / pass_token / gen_time

    后端  收到这 4 个参数后，用「秘钥」对 lot_number 做 HMAC-SHA256 得到 sign_token，
          再把这 5 个值 POST 到极验的二次校验接口：
              POST {api}/validate?captcha_id=<id>
          只有返回 status=success 且 result=success 才算通过。

关键点：
  * 验证结果只能由服务端二次校验判定，**前端回调不能当凭据**；
  * captcha_key（秘钥）绝不能下发到前端，只在服务端参与签名；
  * 每套参数一次性，同一套重放会被极验判 fail（也由我们用 audit 兜底）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request

from .config import CFG

log = logging.getLogger("studio.geetest")

# 极验 v4 官方二次校验地址（可被 GEETEST_API 覆盖，海外节点用）
DEFAULT_API = "https://gcaptcha4.geetest.com"


class GeetestError(RuntimeError):
    """调用极验接口失败（网络 / 返回格式异常）。"""


def configured() -> bool:
    return bool(CFG.geetest_id and CFG.geetest_key)


def _sign(lot_number: str) -> str:
    """sign_token = HMAC-SHA256(lot_number, captcha_key)，十六进制小写。"""
    return hmac.new(CFG.geetest_key.encode("utf-8"),
                    lot_number.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def verify(lot_number: str, captcha_output: str, pass_token: str,
           gen_time: str, ip: str = "") -> tuple[bool, str]:
    """服务端二次校验。返回 (是否通过, 失败原因)。

    ip 只用于日志排查，极验的校验结果与它无关（极验侧自己记了用户 IP）。
    """
    if not configured():
        return False, "人机验证未配置（缺少 GEETEST_ID / GEETEST_KEY），请联系管理员"

    lot_number = (lot_number or "").strip()
    captcha_output = (captcha_output or "").strip()
    pass_token = (pass_token or "").strip()
    gen_time = (gen_time or "").strip()
    if not (lot_number and captcha_output and pass_token and gen_time):
        return False, "人机验证未完成，请重新验证"

    form = {
        "lot_number": lot_number,
        "captcha_output": captcha_output,
        "pass_token": pass_token,
        "gen_time": gen_time,
        "sign_token": _sign(lot_number),
    }
    api = (CFG.geetest_api or DEFAULT_API).rstrip("/")
    url = "%s/validate?%s" % (api, urllib.parse.urlencode({"captcha_id": CFG.geetest_id}))
    body = urllib.parse.urlencode(form, quote_via=urllib.parse.quote).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})

    try:
        with urllib.request.urlopen(req, timeout=8,
                                    context=ssl.create_default_context()) as resp:
            raw = resp.read().decode("utf-8", "ignore")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("极验校验接口不可达：%s（ip=%s）", exc, ip)
        if CFG.geetest_fail_open:
            log.warning("GEETEST_FAIL_OPEN=1，本次人机验证按放行处理")
            return True, ""
        return False, "人机验证服务暂时不可用，请稍后重试"

    try:
        pack = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("极验返回非 JSON：%s", raw[:200])
        if CFG.geetest_fail_open:
            return True, ""
        return False, "人机验证返回异常，请重新验证"

    status = str(pack.get("status") or "")
    result = str(pack.get("result") or "")
    reason = str(pack.get("reason") or "")

    if status == "success" and result == "success":
        return True, ""

    # status=error 一般是 captcha_id 不匹配/参数缺失，属于配置问题，要打日志
    if status != "success":
        log.warning("极验校验请求被拒：status=%s code=%s msg=%s",
                    status, pack.get("code"), pack.get("msg") or reason)
        return False, "人机验证请求异常，请重新验证"

    log.info("极验校验未通过：reason=%s ip=%s", reason or "-", ip)
    return False, "人机验证未通过，请重新验证"


def selfcheck() -> dict:
    """给后台/排查用：只看配置，不发请求。"""
    return {
        "configured": configured(),
        "captcha_id": CFG.geetest_id or "",
        "key_set": bool(CFG.geetest_key),
        "api": CFG.geetest_api or DEFAULT_API,
        "fail_open": bool(CFG.geetest_fail_open),
    }
