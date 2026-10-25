#!/usr/bin/env python
"""SMTP 发信自检工具 —— 配好邮箱后先跑这个，别等到注册页试错。

用法（在 backend 目录下执行，或在项目根目录执行也行）：

    python tools/mailtest.py                      # 只体检，不发信
    python tools/mailtest.py you@example.com      # 体检 + 真发一封测试邮件

它会依次检查：
    1. .env 里的 SMTP_HOST / SMTP_USER / SMTP_PASS 是否都填了
    2. 端口能不能连上（防火墙 / 端口封禁会卡在这一步）
    3. 账号密码能不能登录（这一步失败 90% 是把「登录密码」当成了「授权码」）
    4. 真发一封邮件（只用第 2 个参数时执行）

常见报错的翻译见 docs/邮件配置.md
"""
from __future__ import annotations

import os
import socket
import smtplib
import ssl
import sys

# Windows 控制台默认 GBK，直接 print 中文/emoji 会崩
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # backend/

from app.config import CFG, ROOT_DIR          # noqa: E402
from app import mailer                        # noqa: E402

OK = "  [OK]  "
NO = "  [X]   "
WARN = "  [!]   "


def mask(s: str) -> str:
    if not s:
        return "（空）"
    if len(s) <= 6:
        return s[0] + "*" * (len(s) - 1)
    return s[:3] + "*" * (len(s) - 6) + s[-3:]


def hr(title: str) -> None:
    print("\n" + title)
    print("-" * 58)


def p(*args) -> None:
    print(*args)


def check_config() -> bool:
    hr("1) 读配置")
    env_path = os.path.join(ROOT_DIR, ".env")
    p("    配置文件：%s %s" % (env_path, "" if os.path.isfile(env_path) else "（不存在！）"))
    p("    APP_ENV       = %s" % (CFG.app_env or "（未设置）"))
    p("    SMTP_HOST     = %s" % (CFG.smtp_host or "（空）"))
    p("    SMTP_PORT     = %s" % CFG.smtp_port)
    p("    SMTP_USER     = %s" % (CFG.smtp_user or "（空）"))
    p("    SMTP_PASS     = %s" % mask(CFG.smtp_pass))
    p("    SMTP_FROM     = %s" % (CFG.smtp_from or "（空，将用 SMTP_USER）"))
    p("    SMTP_SSL      = %s" % int(CFG.smtp_ssl))
    p("    SMTP_STARTTLS = %s" % int(CFG.smtp_starttls))
    p("    MAIL_ECHO     = %s" % int(CFG.mail_echo))
    p("    EMAIL_CODE_REQUIRED = %s" % int(CFG.email_code_required))

    missing = [k for k, v in (("SMTP_HOST", CFG.smtp_host),
                              ("SMTP_USER", CFG.smtp_user),
                              ("SMTP_PASS", CFG.smtp_pass)) if not v]
    if missing:
        p(NO, "缺少：%s" % "、".join(missing))
        p("       打开 %s，把这三项填上（参考 docs/邮件配置.md）" % env_path)
        return False
    p(OK, "三项必填都已配置")

    if not CFG.smtp_ssl and not CFG.smtp_starttls:
        p(WARN, "SSL 和 STARTTLS 都关了 —— 多数邮箱会拒收明文登录，除非是内网中继")
    if " " in CFG.smtp_pass:
        p(WARN, "SMTP_PASS 里带空格，注意授权码末尾别误带空格（已自动 strip，一般无碍）")
    return True


def check_tcp() -> bool:
    hr("2) 连端口 %s:%s" % (CFG.smtp_host, CFG.smtp_port))
    try:
        with socket.create_connection((CFG.smtp_host, int(CFG.smtp_port)), timeout=10):
            pass
        p(OK, "端口可连通")
        return True
    except socket.timeout:
        p(NO, "连接超时")
        p("       1) 服务器防火墙 / 云厂商安全组是否放行出站 %s" % CFG.smtp_port)
        p("       2) 换个端口试：465(SSL) / 587(STARTTLS) / 25(多数云主机封禁)")
        return False
    except socket.gaierror:
        p(NO, "域名解析失败，SMTP_HOST 写错了吧（只写域名，别带 https:// 和端口）")
        return False
    except OSError as exc:
        p(NO, "连不上：%s" % exc)
        return False


def check_login() -> smtplib.SMTP | None:
    hr("3) 登录 %s" % CFG.smtp_user)
    ctx = ssl.create_default_context()
    try:
        if CFG.smtp_ssl:
            s = smtplib.SMTP_SSL(CFG.smtp_host, int(CFG.smtp_port), timeout=15, context=ctx)
        else:
            s = smtplib.SMTP(CFG.smtp_host, int(CFG.smtp_port), timeout=15)
            s.ehlo()
            if CFG.smtp_starttls:
                s.starttls(context=ctx)
                s.ehlo()
    except ssl.SSLError as exc:
        p(NO, "TLS 握手失败：%s" % exc)
        p("       465 端口要 SMTP_SSL=1；587 端口要 SMTP_SSL=0 + SMTP_STARTTLS=1")
        return None
    except (smtplib.SMTPException, OSError) as exc:
        p(NO, "建立连接失败：%s" % exc)
        return None

    try:
        s.login(CFG.smtp_user, CFG.smtp_pass)
        p(OK, "登录成功")
        return s
    except smtplib.SMTPAuthenticationError as exc:
        code = exc.smtp_code
        p(NO, "登录被拒（%s）" % code)
        if code in (535, 534):
            p("       账号或密码不对。国内邮箱这里要填【授权码】，不是网页登录密码：")
            p("       QQ 邮箱 → 设置 → 账号 → 开启 SMTP → 生成授权码（16 位）")
            p("       163 邮箱 → 设置 → POP3/SMTP/IMAP → 开启 → 新增授权码")
        elif code == 530:
            p("       需要先做认证/开启 SMTP 服务（邮箱设置里没开）")
        try:
            s.quit()
        except Exception:  # noqa: BLE001
            pass
        return None
    except smtplib.SMTPServerDisconnected as exc:
        # QQ 邮箱密码错时经常直接断连，不回 535，表现成 "Connection unexpectedly closed"
        p(NO, "登录被拒：服务器直接断开了连接（%s）" % exc)
        p("       QQ / 163 邮箱在账号或授权码不对时就是这个反应，优先怀疑 SMTP_PASS：")
        p("       必须填邮箱后台生成的 16 位【授权码】，不是网页登录密码，也别忘了先开启 SMTP 服务")
        return None
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        p(NO, "登录异常：%s" % msg)
        if "unexpectedly closed" in msg.lower():
            p("       同上：账号或授权码不对时服务器会直接断连")
        try:
            s.quit()
        except Exception:  # noqa: BLE001
            pass
        return None


def check_send(s: smtplib.SMTP, to: str) -> bool:
    hr("4) 发测试邮件 → %s" % to)
    try:
        mailer.send_code(to, "888888", "register", 10)
        p(OK, "已交给邮件服务器，去收件箱看看（10 分钟内没到就翻一下垃圾箱）")
        return True
    except mailer.MailError as exc:
        p(NO, "发送失败：%s" % exc)
        msg = str(exc)
        if "550" in msg or "553" in msg:
            p("       发件地址被拒：SMTP_USER 必须和发件人一致，企业邮箱要先在后台绑定该地址")
        elif "554" in msg:
            p("       被判定为垃圾邮件：换正式域名邮箱，或先用 SMTP_USER 自己给自己发")
        elif "timed out" in msg.lower():
            p("       发信超时：多半是出站 25/465 被拦，找云厂商解封或换 587")
        return False
    finally:
        try:
            s.quit()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    to = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    p("=" * 58)
    p("Viora AI · SMTP 发信自检")
    p("=" * 58)

    if not check_config():
        p("\n结论：配置没填全，服务端发码一定会报「邮件服务还没配置」。")
        return 1
    if not check_tcp():
        p("\n结论：网络层就不通，先解决端口/域名，再谈账号密码。")
        return 1
    s = check_login()
    if s is None:
        p("\n结论：连接通了但登录失败，按上面的提示改 SMTP_PASS（授权码）。")
        return 1

    if to:
        ok = check_send(s, to)
        p("\n结论：%s" % ("发信链路完整可用 ✅" if ok else "登录成功但发信被拒，见上面提示"))
        return 0 if ok else 1

    try:
        s.quit()
    except Exception:  # noqa: BLE001
        pass
    p("\n结论：配置与登录都正常 ✅（加一个邮箱地址可再发一封真信验证）")
    p("      例：python tools/mailtest.py you@example.com")
    return 0


if __name__ == "__main__":
    sys.exit(main())
