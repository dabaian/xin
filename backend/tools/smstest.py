#!/usr/bin/env python
"""短信通道自检工具 —— 配好短信宝后先跑这个，别等到注册页试错。

用法（在 backend 目录下执行，或在项目根目录执行也行）：

    python tools/smstest.py                    # 只体检：账号、密钥形态、余额、模板
    python tools/smstest.py 13800138000        # 体检 + 真发一条验证码短信

它会依次检查：
    1. .env 里的 SMSBAO_USER / SMSBAO_KEY 是否都填了
    2. SMSBAO_KEY 的形态对不对
       —— 短信宝要求「平台登录密码的 32 位 MD5」或 ApiKey，
          填登录密码明文会返回 30，这是最常见的坑
    3. 查一次余额（能查到说明账号和密钥都是对的）
    4. 打印实际要发出去的内容，看长度和签名
    5. 用你给的手机号真发一条（只有传了手机号才执行）

返回码对照（接口原样返回的纯文本）：
    0 成功 / 30 密码错误 / 40 账号不存在 / 41 余额不足
    43 IP 地址限制 / 50 内容含敏感词 / 51 手机号码不正确

⚠️ 如果这里显示发送成功但手机收不到，问题不在代码，在短信宝后台：
   国内短信必须先「申请 VIP 通道模板并联系客服报备」，否则通道投递会被拦。
   详见 docs/短信配置.md
"""
from __future__ import annotations

import os
import sys

# Windows 控制台默认 GBK，直接 print 中文/emoji 会崩
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # backend/

from app import sms                          # noqa: E402
from app.config import CFG                   # noqa: E402

OK = "  [OK]  "
NO = "  [X]   "
WARN = "  [!]   "


def hr(title: str) -> None:
    print("\n" + title)
    print("-" * 58)


def p(*args) -> None:
    print(*args)


def check_config() -> bool:
    hr("1. 读取配置")
    p("  短信宝账号     : %s" % (CFG.smsbao_user or "（空）"))
    p("  密钥           : %s" % (sms.mask(CFG.smsbao_key) if CFG.smsbao_key else "（空）"))
    p("  发送接口       : %s" % CFG.smsbao_api)
    p("  产品 ID (g)    : %s" % (CFG.smsbao_goods or "（未指定，走通用产品）"))
    p("  内容模板       : %s" % CFG.sms_template)
    p("  找回密码模板   : %s" % CFG.sms_template_reset)

    if not CFG.smsbao_user or not CFG.smsbao_key:
        p(NO + "SMSBAO_USER / SMSBAO_KEY 没填全，短信发不出去")
        p("        → 在 .env 里补上，格式：")
        p("          SMSBAO_USER=你的短信宝账号")
        p("          SMSBAO_KEY=登录密码的32位MD5")
        return False

    p(OK + "账号和密钥都已填写")
    return True


def check_key() -> bool:
    hr("2. 检查密钥形态")
    k = (CFG.smsbao_key or "").strip()
    if len(k) == 32 and all(c in "0123456789abcdefABCDEF" for c in k):
        p(OK + "是 32 位十六进制，符合「登录密码 MD5」的格式")
        return True
    if len(k) >= 16:
        p(WARN + "不是 32 位 MD5，当作 ApiKey 处理（长度 %d）" % len(k))
        p("        如果你填的是登录密码明文，接口会返回 30")
        return True
    p(NO + "密钥太短（%d 位），既不像 MD5 也不像 ApiKey" % len(k))
    return False


def check_balance() -> bool:
    hr("3. 查询余额（同时验证账号密码是否正确）")
    r = sms.balance()
    if r.get("ok"):
        p(OK + "账号密码验证通过")
        p("        已发送 %s 条，剩余 %s 条" % (r.get("sent", "?"), r.get("balance", "?")))
        try:
            if int(r.get("balance") or 0) <= 10:
                p(WARN + "余额不足 10 条，充值后注册量上来会发不出去")
        except (TypeError, ValueError):
            pass
        return True
    p(NO + "查询失败：%s" % r.get("error", "未知错误"))
    p("        → 30 说明密钥不对（要填登录密码的 MD5，不是明文）")
    p("        → 40 说明账号不存在")
    return False


def check_template() -> bool:
    hr("4. 检查短信内容")
    for purpose, label in (("register", "注册"), ("reset", "找回密码")):
        text = sms.render(purpose, "123456", 5)
        n = len(text)
        seg = 1 if n <= sms.ONE_SMS_CHARS else -(-n // sms.ONE_SMS_CHARS)
        flag = OK if n <= sms.ONE_SMS_CHARS else WARN
        p("%s%s模板 %d 字，按 %d 条计费" % (flag, label, n, seg))
        p("        %s" % text)
        if "【" not in text:
            p(WARN + "内容里没有签名【】，运营商可能把它当成垃圾短信")
        if "{code}" in (CFG.sms_template or "") and purpose == "register":
            pass
    return True


def check_send(mobile: str) -> bool:
    hr("5. 真发一条到 %s" % sms.mask(mobile))
    p("  发送中，请稍候…")
    try:
        info = sms.send(mobile, "123456", "register", 5)
    except sms.SmsError as exc:
        p(NO + str(exc))
        return False
    if info.get("echo"):
        p(WARN + "当前是回显模式（没配短信宝 / SMS_ECHO=1），没有真发")
        return True
    p(OK + "短信宝已接收（返回 0）")
    p("        请查看手机，内容里的验证码是 123456")
    p("        1 分钟内没收到，大概率是通道模板还没报备，联系短信宝客服")
    return True


def main() -> int:
    print("=" * 60)
    print("  Viora AI · 短信通道自检")
    print("=" * 60)

    if not check_config():
        return 2
    if not check_key():
        return 2
    if not check_balance():
        return 3
    check_template()

    if len(sys.argv) > 1:
        mobile = sys.argv[1].strip()
        if not check_send(mobile):
            return 4
    else:
        hr("5. 真发测试（未执行）")
        p("  想真发一条看效果，带上手机号再跑一次：")
        p("      python tools/smstest.py 你的手机号")
        p("  会真的扣 1 条余额，但这是确认通道可用的唯一办法。")

    print("\n" + "=" * 60)
    print("  自检结束")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
