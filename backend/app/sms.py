"""短信宝（smsbao）短信通道：注册 / 找回密码验证码。

官方文档：https://www.smsbao.com/openapi/213.html
接口形态（GET，返回纯文本状态码，不是 JSON）：

    https://api.smsbao.com/sms?u=<账号>&p=<密钥>&m=<手机号>&c=<UTF-8 URL编码的内容>

参数要点（照文档逐条核对过）
------------------------------
u  必填  平台注册的账号
p  必填  平台登录密码的 **32 位 MD5**（不区分大小写），或后台生成的 ApiKey。
        注意：这里**不能填登录密码明文** —— 填错会返回 30。
g  选填  专用通道的产品 ID，不填走「通用短信产品」
m  必填  手机号；群发用逗号分隔，一次不超过 99 个
c  必填  短信内容，**必须 UTF-8 URL encode**，签名【】写在内容里

返回：第一行为 '0' 即成功，其余为错误码
      30 错误密码 / 40 账号不存在 / 41 余额不足 / 43 IP 地址限制
      50 内容含敏感词 / 51 手机号码不正确

⚠️ 上线前置条件（文档明确要求，代码管不了）
   发送国内短信必须先在短信宝后台**申请 VIP 通道模板并联系客服报备**，
   否则会出现「接口返回成功但用户收不到」或直接被拦截。
   另外网页注册场景官方要求必须配图形验证 —— 本项目走极验 v4，已满足。

三条铁律（和 mailer.py 一致）
------------------------------
1. 配了账号+密钥 → 真发短信；
2. 没配但显式开了 `SMS_ECHO=1`（且非生产） → 验证码回显到页面，本地调试用；
3. 其余情况 → 直接抛 SmsError，绝不静默吞掉，也绝不假装发送成功。
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request

from . import guard
from .config import CFG

logger = logging.getLogger("muse.sms")

# 短信宝返回码 → 人话
RET = {
    "-1": "短信宝参数不全或内容超长",
    "0": "发送成功",
    "30": "短信宝密码错误：SMSBAO_KEY 要填平台登录密码的 32 位 MD5（或 ApiKey），不是登录密码明文",
    "40": "短信宝账号不存在，检查 SMSBAO_USER",
    "41": "短信宝账户余额不足，请先充值",
    "42": "短信宝账户已过期",
    "43": "短信宝 IP 地址限制：后台绑定了固定 IP，当前服务器 IP 不在白名单",
    "50": "内容含敏感词被拦截：检查模板文案，或模板还没在短信宝后台报备",
    "51": "手机号码不正确",
}

# 单条短信 70 字以内按 1 条计费，超出按长短信拆分计费。
ONE_SMS_CHARS = 70


class SmsError(RuntimeError):
    """发短信失败（配置缺失、账号问题、余额不足、被拦截等）。"""


def configured() -> bool:
    return bool(CFG.smsbao_user and CFG.smsbao_key)


def key_looks_valid() -> bool:
    """粗查 SMSBAO_KEY 的形态：32 位 MD5，或后台生成的 ApiKey。

    文档明确要求 p 是「密码 MD5（32 位）」而不是登录密码明文。
    这里只做形态判断 —— 真验一次得靠 send()（填错会返回 30）。
    """
    k = (CFG.smsbao_key or "").strip()
    if len(k) == 32 and all(c in "0123456789abcdefABCDEF" for c in k):
        return True
    return len(k) >= 16          # ApiKey 一般是较长的随机串


def echo_mode() -> bool:
    """没配短信账号时是否允许把验证码回显到页面。

    只认一个条件：显式配置 `SMS_ECHO=1`，并且当前不是生产环境。
    """
    if configured():
        return False
    return guard.sms_echo_on()


def render(purpose: str, code: str, minutes: int = 5) -> str:
    """按用途套模板。模板里的 {code} / {minutes} 会被替换。"""
    tpl = CFG.sms_template
    if purpose == "reset":
        tpl = CFG.sms_template_reset or tpl
    elif purpose == "login":
        tpl = CFG.sms_template_login or tpl
    text = (tpl or "").replace("{code}", code).replace("{minutes}", str(minutes))
    return text.strip()


def send(mobile: str, code: str, purpose: str = "register", minutes: int = 5) -> dict:
    """发一条验证码短信。成功返回 {"kept": bool, "chars": int}。

    失败一律抛 SmsError，调用方（routers/auth.py）翻译成 HTTP 503。
    """
    mobile = (mobile or "").strip()
    content = render(purpose, code, minutes)

    if not content:
        raise SmsError("短信模板是空的（SMS_TEMPLATE），请检查 .env")
    if "{code}" not in (CFG.sms_template or "") and purpose == "register":
        logger.warning("SMS_TEMPLATE 里没有 {code} 占位符，用户收到的短信不含验证码")

    if not configured():
        if echo_mode():
            logger.warning("[验证码回显] 未配置短信宝，%s 的验证码 %s（%d 分钟内有效）",
                           mask(mobile), code, minutes)
            return {"echo": True, "chars": len(content), "text": content}
        raise SmsError("短信服务还没配置（SMSBAO_USER / SMSBAO_KEY），"
                       "暂时发不出验证码，请联系管理员")

    params = {"u": CFG.smsbao_user, "p": CFG.smsbao_key, "m": mobile, "c": content}
    if CFG.smsbao_goods:
        params["g"] = CFG.smsbao_goods
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote, encoding="utf-8")
    url = "%s?%s" % (CFG.smsbao_api, query)
    req = urllib.request.Request(url, headers={"User-Agent": "VioraAI/1.0"})

    # 打日志时把 p 和 m 都脱敏，别把密钥写进日志文件
    safe = {k: (v if k in ("u", "g") else ("***" if k == "p" else mask(v)))
            for k, v in params.items()}
    logger.info("短信宝请求：%s %s", CFG.smsbao_api, safe)

    try:
        with urllib.request.urlopen(req, timeout=CFG.sms_timeout) as resp:
            body = resp.read().decode("utf-8", "replace").strip()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("短信宝请求失败：%s", exc)
        raise SmsError("短信网关连接失败（%s），请稍后重试" % exc) from exc

    code_no = body.split()[0] if body else ""
    if code_no != "0":
        why = RET.get(code_no, "未知错误：%s" % (body or "空响应"))
        logger.warning("短信宝返回 %s：%s（%s）", code_no, why, mask(mobile))
        raise SmsError("短信发送失败：%s" % why)

    logger.info("验证码短信已发送：%s（%s，%d 字）", mask(mobile), purpose, len(content))
    return {"echo": False, "chars": len(content),
            "segments": 1 if len(content) <= ONE_SMS_CHARS else -(-len(content) // ONE_SMS_CHARS)}


def balance() -> dict:
    """查短信宝余额（自检用，不参与业务）。文档要求调用频率 ≤ 1 次/分钟。"""
    if not configured():
        return {"ok": False, "error": "短信宝未配置"}
    query = urllib.parse.urlencode({"u": CFG.smsbao_user, "p": CFG.smsbao_key})
    url = "%s?%s" % (CFG.smsbao_query, query)
    try:
        with urllib.request.urlopen(url, timeout=CFG.sms_timeout) as resp:
            body = resp.read().decode("utf-8", "replace").strip()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    # 成功格式：第一行 0，第二行「发送条数,剩余条数」
    parts = [ln.strip() for ln in body.split("\n") if ln.strip()]
    if parts and parts[0] == "0":
        pair = (parts[1] if len(parts) > 1 else "").split(",")
        return {"ok": True,
                "sent": pair[0] if pair else "",
                "balance": pair[1] if len(pair) > 1 else ""}
    return {"ok": False, "error": RET.get(parts[0] if parts else "", body)}


def mask(mobile: str) -> str:
    """138****8000 —— 日志里不写完整号码。"""
    m = (mobile or "").strip()
    return m[:3] + "****" + m[-4:] if len(m) >= 7 else m


def selfcheck() -> dict:
    """给后台/前端自检用：不真发短信，只看配置是否齐全。"""
    return {
        "configured": configured(),
        "key_valid": key_looks_valid(),
        "user": CFG.smsbao_user or "",
        "api": CFG.smsbao_api,
        "goods": CFG.smsbao_goods,
        "echo_mode": echo_mode(),
        "ttl": int(CFG.sms_code_ttl),
        "template": CFG.sms_template,
        "template_reset": CFG.sms_template_reset,
        "chars": len(render("register", "000000", 5)),
    }
