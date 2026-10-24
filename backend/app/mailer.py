"""SMTP 发信：注册 / 登录验证码。

没配 SMTP 时进入「演示模式」：验证码只写日志，并随接口响应回传给前端，
方便本地把流程跑通。生产环境（`APP_ENV=prod` 且没显式开 `MAIL_ECHO`）不回传，
直接报错，避免线上被人套出验证码。
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

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
    """没配 SMTP 时是否允许把验证码回显出来。"""
    if configured():
        return False
    return bool(CFG.mail_echo) or CFG.app_env != "prod"


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
            logger.warning("[演示模式] 未配置 SMTP，%s 验证码 %s（%d 分钟内有效）",
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
    except (smtplib.SMTPException, OSError) as exc:
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
