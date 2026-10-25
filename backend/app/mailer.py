"""SMTP 发信：注册 / 登录验证码。

验证码只有两条路：
    1. 配了 SMTP        → 真发到邮箱；
    2. 没配但显式开了 MAIL_ECHO（仅非生产环境） → 回显到页面，本地调试用；
    3. 其余情况          → 直接报错，绝不静默吞掉。

注意：早期版本有「非生产环境自动回显」的兜底，等于只要 APP_ENV 不是 prod，
任何人注册都能直接看到验证码。这个口子已经堵掉 —— 现在必须显式写
`MAIL_ECHO=1`，且生产环境写这个会被启动守卫拦下（见 guard.py）。
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from . import guard
from .config import CFG

logger = logging.getLogger("muse.mail")

_SUBJECT = {
    "register": "【Viora AI】注册验证码",
    "login": "【Viora AI】登录验证码",
}
_TITLE = {
    "register": "注册账号",
    "login": "登录账号",
}


class MailError(RuntimeError):
    """发信失败（配置缺失、连接失败、被拒收等）。"""


def configured() -> bool:
    return bool(CFG.smtp_host and CFG.smtp_user and CFG.smtp_pass)


def echo_mode() -> bool:
    """没配 SMTP 时是否允许把验证码回显出来。

    只认一个条件：显式配置了 MAIL_ECHO=1，并且当前不是生产环境。
    不再有「非生产环境自动回显」的兜底。
    """
    if configured():
        return False
    return guard.mail_echo_on()


def send_code(to: str, code: str, purpose: str, minutes: int) -> None:
    """把验证码发到指定邮箱；演示模式下只写日志。"""
    act = _TITLE.get(purpose, "验证")
    subject = "%s：%s" % (_SUBJECT.get(purpose, "【Viora AI】验证码"), code)

    text = (
        "你正在%s，验证码是：%s\n\n"
        "该验证码 %d 分钟内有效，只能使用一次。\n"
        "如果这不是你本人的操作，忽略这封邮件即可，账号不会有任何变化。\n\n"
        "—— Viora AI"
    ) % (act, code, minutes)

    html = """<!DOCTYPE html>
<html><body style="margin:0;padding:24px;background:#F5F6F8;font-family:-apple-system,
'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif">
  <div style="max-width:520px;margin:0 auto;background:#fff;border-radius:14px;overflow:hidden;
border:1px solid #E9EBEF">
    <div style="padding:22px 26px;background:linear-gradient(135deg,#FF7A45,#FF5A1F);color:#fff">
      <div style="font-size:17px;font-weight:700;letter-spacing:.2px">Viora AI</div>
      <div style="font-size:12.5px;opacity:.85;margin-top:4px">AI 视频生成工作台</div>
    </div>
    <div style="padding:26px">
      <p style="margin:0 0 14px;font-size:14.5px;color:#101319">你正在<strong>%s</strong>，
        请使用下面的验证码：</p>
      <div style="margin:18px 0;padding:16px;text-align:center;background:#FFF4EF;
border:1px dashed #FFC4AE;border-radius:12px">
        <span style="font-size:32px;font-weight:800;letter-spacing:10px;color:#E24E12;
font-family:ui-monospace,Menlo,Consolas,monospace">%s</span>
      </div>
      <p style="margin:0 0 6px;font-size:13px;color:#5B6270">验证码 %d 分钟内有效，只能使用一次。</p>
      <p style="margin:0;font-size:13px;color:#5B6270">如果这不是你本人的操作，忽略这封邮件即可，
        账号不会有任何变化。</p>
    </div>
    <div style="padding:14px 26px;border-top:1px solid #E9EBEF;font-size:11.5px;color:#9AA1AE">
      此邮件由系统自动发送，请勿直接回复。
    </div>
  </div>
</body></html>""" % (act, code, minutes)

    if not configured():
        if echo_mode():
            logger.warning("[验证码回显] 未配置 SMTP，%s 的验证码 %s（%d 分钟内有效）",
                           to, code, minutes)
            return
        raise MailError("邮件服务还没配置（SMTP_HOST / SMTP_USER / SMTP_PASS），"
                        "暂时发不出验证码，请联系管理员")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((CFG.app_name or "Viora AI", CFG.smtp_from or CFG.smtp_user))
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    try:
        ctx = ssl.create_default_context()
        if CFG.smtp_ssl:
            with smtplib.SMTP_SSL(CFG.smtp_host, CFG.smtp_port, timeout=15, context=ctx) as s:
                s.login(CFG.smtp_user, CFG.smtp_pass)
                s.send_message(msg)
        else:
            with smtplib.SMTP(CFG.smtp_host, CFG.smtp_port, timeout=15) as s:
                s.ehlo()
                if CFG.smtp_starttls:
                    s.starttls(context=ctx)
                    s.ehlo()
                s.login(CFG.smtp_user, CFG.smtp_pass)
                s.send_message(msg)
        logger.info("验证码邮件已发送：%s（%s）", to, purpose)
    except smtplib.SMTPAuthenticationError as exc:
        logger.warning("SMTP 登录被拒（%s）：%s", exc.smtp_code, exc.smtp_error)
        raise MailError("邮件服务器拒绝登录，请检查 SMTP_USER / SMTP_PASS 是否为"
                        "邮箱后台生成的授权码（不是登录密码）") from exc
    except smtplib.SMTPServerDisconnected as exc:
        # QQ / 163 在账号或授权码不对时直接断连，不回 535
        logger.warning("SMTP 连接被断开：%s", exc)
        raise MailError("邮件服务器断开了连接，通常是 SMTP_PASS 填了登录密码而不是授权码，"
                        "或邮箱后台没开启 SMTP 服务") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        logger.warning("收件人被拒：%s", exc.recipients)
        raise MailError("收件地址被拒绝，确认邮箱地址是否正确") from exc
    except ssl.SSLError as exc:
        logger.warning("SMTP TLS 失败：%s", exc)
        raise MailError("邮件服务器 TLS 握手失败，检查 SMTP_PORT 与 SMTP_SSL 是否匹配"
                        "（465 配 SSL=1，587 配 SSL=0 + STARTTLS=1）") from exc
    except (smtplib.SMTPException, OSError) as exc:
        logger.warning("SMTP 发送失败：%s", exc)
        raise MailError("邮件发送失败：%s" % exc) from exc


def selfcheck() -> dict:
    """给后台自检用：不真发信，只看配置是否齐全。"""
    return {
        "configured": configured(),
        "host": CFG.smtp_host or "",
        "port": CFG.smtp_port,
        "user": CFG.smtp_user or "",
        "ssl": bool(CFG.smtp_ssl),
        "starttls": bool(CFG.smtp_starttls),
        "echo_mode": echo_mode(),
        "required": bool(CFG.email_code_required),
    }
