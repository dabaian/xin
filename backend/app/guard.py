"""上线守卫：所有「模拟 / 演示 / 测试」能力都必须关在开发环境里。

这个模块只干一件事：**在生产环境下，把不该出现的东西拦在启动之前**。

为什么不只在文档里写「上线前记得关掉」——
    模拟支付白送积分，模拟邮件等于任何人都能拿到任意邮箱的验证码。
    这两件都是「漏改一行配置」就能造成的资损和入侵。
    所以必须让程序自己拒绝启动，不能靠人记。

三道闸
------
1. 生产环境下，`ALIPAY_MOCK` / `MAIL_ECHO` / `PAYMENT_CHANNEL=mock`
   只要有一个开着，进程直接退出，绝不带着漏洞跑起来。
2. 生产环境下模拟通道根本不注册，「模拟到账」接口不存在于路由表里
   （不是返回 404，是压根没这条路由）。
3. 生产环境缺必需配置（上游密钥 / 管理员密码 / 支付参数 / SMTP）
   也拒绝启动 —— 否则线上表现是「页面能打开，但什么都干不了」，
   排查起来比直接起不来痛苦得多。

逃生门
------
不想开充值，就把 `PAYMENT_CHANNEL=none` 显式写上，这是正规出口，
不是绕过检查。
"""
from __future__ import annotations

import os
import sys

from .config import CFG

_PROD_ALIASES = ("prod", "production", "online", "release")


def is_prod() -> bool:
    return (CFG.app_env or "").strip().lower() in _PROD_ALIASES


def mock_allowed() -> bool:
    """当前环境是否允许模拟能力（支付 / 邮件回显）。

    生产环境恒为 False —— 这是整个模块的核心断言，任何地方要判断
    「能不能开模拟」，都只认这一个函数。
    """
    return not is_prod()


def payment_off() -> bool:
    want = (CFG.payment_channel or "").strip().lower()
    return want in ("none", "off", "0", "disabled", "close")


def alipay_mock_on() -> bool:
    return bool(CFG.alipay_mock) and mock_allowed()


def mail_echo_on() -> bool:
    return bool(CFG.mail_echo) and mock_allowed()


# ------------------------------------------------------------------ 检查项
def violations() -> list[tuple[str, str]]:
    """生产环境下的阻断级问题：[(配置项, 怎么改)]，空列表表示通过。"""
    if not is_prod():
        return []

    bad: list[tuple[str, str]] = []

    if CFG.alipay_mock:
        bad.append(("ALIPAY_MOCK=1",
                    "生产环境禁止模拟支付（等于白送积分）→ 改成 ALIPAY_MOCK=0"))
    if CFG.mail_echo:
        bad.append(("MAIL_ECHO=1",
                    "生产环境禁止回显验证码（等于绕过邮箱验证）→ 删除这行或改成 0"))
    if (CFG.payment_channel or "").strip().lower() == "mock":
        bad.append(("PAYMENT_CHANNEL=mock",
                    "生产环境禁止模拟通道 → 改成 alipay，或写 none 关闭充值"))

    if not CFG.muse_key:
        bad.append(("MUSE_KEY=(空)",
                    "没配上游号池密钥，所有生成任务都会失败 → 填上密钥"))

    if not CFG.admin_password:
        bad.append(("ADMIN_PASSWORD=(空)",
                    "没配管理员密码会落到内置默认口令 → 设一个强密码"))

    if CFG.email_code_required and not mail_ready():
        bad.append(("SMTP_*=未配置",
                    "邮箱验证码是注册/登录的必经步骤，但 SMTP 没配，"
                    "等于注册全挂 → 配好 SMTP_HOST/USER/PASS，"
                    "或设 EMAIL_CODE_REQUIRED=0 改为只校验图形码"))

    if not payment_off() and not CFG.alipay_ready:
        bad.append(("ALIPAY_APP_ID/密钥=(空)",
                    "支付通道没配齐，用户点充值会报错 → "
                    "配好商户参数，或写 PAYMENT_CHANNEL=none 明确关闭充值"))

    return bad


def warnings() -> list[str]:
    """不阻断启动，但上线前该知道的事。"""
    out: list[str] = []
    if not is_prod():
        on = [n for n, v in (("ALIPAY_MOCK", CFG.alipay_mock),
                             ("MAIL_ECHO", CFG.mail_echo)) if v]
        out.append("当前不是生产环境（APP_ENV=%s），启动守卫不介入；"
                   "模拟开关由 ALIPAY_MOCK / MAIL_ECHO 单独控制，当前%s"
                   % (CFG.app_env or "未设置",
                      "均为关闭" if not on else "开启项：" + "、".join(on)))
        return out

    if not mail_ready() and not CFG.email_code_required:
        out.append("邮箱验证码已关闭（EMAIL_CODE_REQUIRED=0），注册只校验人机验证")
    from . import captcha as captcha_mod

    if captcha_mod.provider() == "off":
        out.append("人机验证已关闭（CAPTCHA_PROVIDER=off，或极验未配置且关了图形码），"
                   "注册接口可能被脚本刷")
    elif captcha_mod.provider() == "image":
        out.append("当前用自绘图形验证码（极验未配置）——能挡住低级脚本，"
                   "挡不住打码平台。建议配 GEETEST_ID / GEETEST_KEY")
    if not CFG.site_base.startswith("https://"):
        out.append("站点地址不是 https（%s）：Cookie 不会被标记 Secure，"
                   "支付宝回调也要求 https" % CFG.site_base)
    if "*" in CFG.cors_list:
        out.append("CORS_ORIGINS=* ：带 Cookie 的跨域请求会失效，"
                   "建议写具体域名")
    return out


def mail_ready() -> bool:
    return bool(CFG.smtp_host and CFG.smtp_user and CFG.smtp_pass)


# ------------------------------------------------------------------ 启动闸门
_BAR = "=" * 68


def check_boot() -> None:
    """导入阶段调用。生产环境有阻断项就直接退出，不进入初始化。"""
    problems = violations()
    if not problems:
        return

    lines = [
        "",
        _BAR,
        "  启动被拒绝：生产环境下发现 %d 项必须修正的配置" % len(problems),
        _BAR,
        "",
    ]
    for i, (item, how) in enumerate(problems, 1):
        lines.append("  %d. %s" % (i, item))
        lines.append("     → %s" % how)
        lines.append("")
    lines += [
        _BAR,
        "  这些都是「漏改一行就出事」的项，所以由程序拦下来，不靠人工检查。",
        "  改完 %s 后重新启动即可。" % _env_file(),
        _BAR,
        "",
    ]
    # 这时日志系统可能还没配好，直接写 stderr 最可靠
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()
    raise SystemExit(2)


def _env_file() -> str:
    for name in (".env",):
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        p = os.path.join(here, name)
        if os.path.isfile(p):
            return p
    return ".env"


def banner() -> str:
    """启动横幅：一眼看清当前是「真收钱」还是「模拟」。"""
    from .payments import gateway

    active = gateway.resolve_code()
    adapter = gateway.get(active) if active else None
    env = (CFG.app_env or "未设置").upper()

    if adapter is None:
        pay = "充值已关闭（PAYMENT_CHANNEL=none 或支付参数未配置）"
    elif adapter.is_mock():
        pay = "【模拟支付】不会真收钱，仅限本地验收"
    else:
        pay = "%s（真实收款）" % adapter.name

    mail = "SMTP 已配置" if mail_ready() else (
        "【验证码回显】未配置 SMTP，验证码直接显示在页面上" if mail_echo_on()
        else "SMTP 未配置，邮箱验证码将发不出去")

    return "%s · 环境 %s · %s · %s" % (CFG.app_name, env, pay, mail)
