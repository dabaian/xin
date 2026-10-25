"""注册 / 登录 / 找回密码 / 会话。

账号体系：**手机号 + 密码**

    注册      手机号 + 短信验证码 + 密码
    登录      手机号 + 密码（老用户的邮箱 / 用户名也还能登）
    找回密码  手机号 + 短信验证码 → 设置新密码

为什么注册要短信验证码而不是邮箱：
    手机号是实名资产，一人一号，注册即完成手机绑定；找回密码也走同一个通道，
    用户不用记「当年注册用的是哪个邮箱」。短信通道见 sms.py。

为什么数据库里还留着 email 字段：
    1) 老用户的数据不能丢，他们仍可用邮箱 + 密码登录；
    2) users.email 是 NOT NULL UNIQUE，手机号注册时写入 `138xxx@mobile.invalid`
       这个占位值（.invalid 是 RFC 2606 保留域名，永远不可能是真实邮箱），
       这样不必重建表，也不会和真实邮箱撞车。
"""
from __future__ import annotations

import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import captcha, db, ledger, sms, smscode
from ..config import CFG
from ..deps import COOKIE_NAME, client_ip, current_user
from ..security import (captcha_limiter, hash_password, login_limiter,
                        register_limiter, sms_limiter, new_token, token_hash,
                        verify_password)

router = APIRouter(prefix="/api/auth", tags=["auth"])

SESSION_DAYS = 14
DEVICE_DAYS = 180

# 中国大陆手机号：1 开头，第二位 3-9，共 11 位
_MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PLACEHOLDER_DOMAIN = "mobile.invalid"


# ------------------------------------------------------------------ 入参
class CaptchaMixin(BaseModel):
    """人机验证参数（极验 v4 四件套，或自绘图形码 id + 答案）。"""

    lot_number: str = ""
    captcha_output: str = ""
    pass_token: str = ""
    gen_time: str = ""
    captcha_id: str = ""
    captcha_code: str = ""


class RegisterIn(CaptchaMixin):
    # 长度交给 _check_mobile / 下面的 if 判断，不用 pydantic 的 min_length：
    # 否则用户填 5 位数字会拿到 "mobile String should have at least 6 characters"
    # 这种英文报错，不如「手机号格式不正确」好懂。
    mobile: str = Field(default="", max_length=20)
    password: str = Field(default="", max_length=64)
    sms_code: str = ""
    username: str = Field(default="", max_length=32)
    invite: str = ""


class SmsCodeIn(CaptchaMixin):
    mobile: str = Field(default="", max_length=20)
    purpose: str = "register"      # register / reset / login


class LoginIn(BaseModel):
    account: str = Field(default="", max_length=120)
    password: str = ""
    remember: bool = True


class ResetIn(BaseModel):
    mobile: str = Field(default="", max_length=20)
    sms_code: str = ""
    new_password: str = Field(default="", max_length=64)


class PasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=64)


# ------------------------------------------------------------------ 工具
def _placeholder_email(mobile: str) -> str:
    return "%s@%s" % (mobile, _PLACEHOLDER_DOMAIN)


def _is_placeholder(email: str) -> bool:
    return (email or "").lower().endswith("@" + _PLACEHOLDER_DOMAIN)


def _public_email(email: str) -> str:
    """占位邮箱不给前端看到，否则导航栏会显示 138xxx@mobile.invalid。"""
    return "" if _is_placeholder(email) else (email or "")


def _check_mobile(raw: str) -> str:
    mobile = smscode.normalize(raw)
    if not _MOBILE_RE.match(mobile):
        raise HTTPException(400, "手机号格式不正确，请填写 11 位中国大陆手机号")
    return mobile


def _is_https(request: Request) -> bool:
    """按本次请求的真实协议决定 Cookie 是否加 Secure。

    不能用 PUBLIC_BASE 判断：证书还没签下来时用 http 访问，
    Secure Cookie 不会被浏览器回传，表现成「登录成功但立刻又未登录」。
    """
    if request.url.scheme == "https":
        return True
    xf = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return xf == "https"


def _issue_session(conn, user_id: int, request: Request, response: Response,
                   days: int = SESSION_DAYS) -> str:
    token = new_token(32)
    conn.execute("INSERT INTO sessions(token_hash, user_id, ua, ip, created_at, expires_at) "
                 "VALUES(?,?,?,?,?,?)",
                 (token_hash(token), user_id,
                  (request.headers.get("user-agent") or "")[:200],
                  client_ip(request), db.now(), db.now_plus(days * 1440)))
    conn.commit()
    response.set_cookie(COOKIE_NAME, token, max_age=days * 86400, httponly=True,
                        samesite="lax", secure=_is_https(request))
    return token


def _profile(conn, user) -> dict:
    w = ledger.balance(conn, user["id"])
    mobile = (user["mobile"] or "") if "mobile" in user.keys() else ""
    email = _public_email(user["email"])
    fallback = mobile[-4:] if mobile else (email.split("@")[0] if email else str(user["id"]))
    return {
        "id": user["id"],
        "mobile": mobile,
        "email": email,
        "account": mobile or email,           # 前端统一显示这个
        "username": user["username"] or fallback,
        "role": user["role"],
        "status": user["status"],
        "created_at": user["created_at"],
        "wallet": w,
    }


# ------------------------------------------------------------------ 人机验证 / 通道状态
@router.get("/captcha")
def get_captcha(request: Request, conn=Depends(db.get_db)):
    """自绘图形验证码（仅在 provider=image 时有效）。

    走极验时这里返回 enabled=False，前端据此不渲染图片那一栏。
    """
    ip = client_ip(request) or "unknown"
    if not captcha_limiter.hit(ip):
        return {"ok": False, "error": "验证码请求太频繁，请稍后再试"}
    return captcha.issue(conn)


@router.get("/captcha/config")
def captcha_config():
    """前端启动时读一次，决定用人机验证的哪种形态。

    captcha_id 是极验的「验证 ID」，本来就暴露在网页源码里，不是密钥。
    真正的 GEETEST_KEY（秘钥）只在服务端参与签名，任何接口都不会返回它。
    """
    mode = captcha.provider()
    return {
        "ok": True,
        "enabled": mode != "off",
        "provider": mode,                 # geetest / image / off
        "captcha_id": CFG.geetest_id if mode == "geetest" else "",
        "product": "bind",
        "ttl": int(CFG.captcha_ttl),
    }


@router.get("/sms/config")
def sms_config():
    """前端用来显示「短信通道是否可用」「验证码几分钟有效」。"""
    info = sms.selfcheck()
    return {
        "ok": True,
        "configured": info["configured"],
        "echo_mode": info["echo_mode"],
        "ttl": int(CFG.sms_code_ttl),
        "ttl_minutes": smscode.ttl_minutes(),
        "cooldown": smscode.COOLDOWN,
    }


# ------------------------------------------------------------------ 发短信验证码
@router.post("/sms-code")
def send_sms_code(data: SmsCodeIn, request: Request, conn=Depends(db.get_db)):
    """发送短信验证码。

    purpose=register 用于注册，reset 用于找回密码（login 预留）。
    顺序：格式 → 账号存在性 → 人机验证 → 频控 → 真发。
    把「存在性」放在人机验证之前，用户少做一次滑块；注册场景本来就要告知
    「这个号已经注册过了」，不存在额外泄露。
    """
    ip = client_ip(request) or "unknown"
    mobile = _check_mobile(data.mobile)
    purpose = data.purpose if data.purpose in ("reset", "login") else "register"

    if not sms_limiter.hit(ip):
        raise HTTPException(429, "获取验证码太频繁，请稍后再试")

    row = conn.execute("SELECT id FROM users WHERE mobile = ?", (mobile,)).fetchone()
    if purpose == "register" and row:
        raise HTTPException(409, "该手机号已注册，请直接登录或找回密码")
    if purpose in ("reset", "login") and not row:
        raise HTTPException(404, "该手机号还没有注册，请先注册账号")

    # 人机验证：挡掉「脚本换号刷短信」这种直接烧钱的攻击
    ok, why = captcha.verify_any(conn, data, ip=ip)
    if not ok:
        raise HTTPException(400, why)

    code, err = smscode.issue(conn, mobile, purpose, ip)
    if err:
        raise HTTPException(429, err)

    minutes = smscode.ttl_minutes()
    try:
        info = sms.send(mobile, code, purpose, minutes)
    except sms.SmsError as exc:
        # 发失败就把这张码作废，否则用户要等 60 秒冷却才能重试
        conn.execute("DELETE FROM sms_codes WHERE mobile=? AND purpose=? AND used=0",
                     (mobile, purpose))
        conn.commit()
        raise HTTPException(503, str(exc)) from exc

    db.audit(conn, None, "sms.code", "mobile", sms.mask(mobile), ip=ip,
             after={"purpose": purpose, "delivered": sms.configured()})
    conn.commit()

    resp = {"ok": True, "ttl": int(CFG.sms_code_ttl), "ttl_minutes": minutes,
            "cooldown": smscode.COOLDOWN, "delivered": sms.configured(),
            "mobile": sms.mask(mobile)}
    # 本地调试（未配短信宝且显式开了 SMS_ECHO）：把码回传，省得去翻日志
    if info.get("echo"):
        resp["echo"] = True
        resp["dev_code"] = code
    return resp


# ------------------------------------------------------------------ 注册
@router.post("/register")
def register(data: RegisterIn, request: Request, response: Response,
             conn=Depends(db.get_db)):
    if not CFG.allow_register:
        raise HTTPException(403, "当前已关闭注册，请联系客服开通")
    ip = client_ip(request)
    if not register_limiter.hit(ip or "unknown"):
        raise HTTPException(429, "注册太频繁了，请稍后再试")

    mobile = _check_mobile(data.mobile)
    if len(data.password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if conn.execute("SELECT id FROM users WHERE mobile = ?", (mobile,)).fetchone():
        raise HTTPException(409, "该手机号已注册，请直接登录")

    # 短信验证码是唯一的「手机号归属」凭据，校验通过即消费
    passed, why = smscode.verify(conn, mobile, "register", data.sms_code)
    if not passed:
        raise HTTPException(400, why)

    username = (data.username or "").strip() or ("用户" + mobile[-4:])
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO users(email, mobile, username, password_hash, role, status, "
            "signup_ip, created_at) VALUES(?,?,?,?,'user','active',?,?)",
            (_placeholder_email(mobile), mobile, username[:32],
             hash_password(data.password), ip, db.now()))
        uid = cur.lastrowid
        ledger.ensure_wallet(conn, uid)
        if CFG.signup_bonus > 0:
            ledger.recharge(conn, uid, CFG.signup_bonus, "signup", str(uid),
                            "新用户注册赠送", count_recharge=False)
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(409, "该手机号已注册，请直接登录") from exc
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, "注册失败：%s" % exc) from exc

    user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    token = _issue_session(conn, uid, request, response)
    db.audit(conn, uid, "register", "user", str(uid), ip=ip,
             after={"mobile": sms.mask(mobile)})
    conn.commit()
    return {"ok": True, "token": token, "user": _profile(conn, user)}


# ------------------------------------------------------------------ 登录
@router.post("/login")
def login(data: LoginIn, request: Request, response: Response,
          conn=Depends(db.get_db)):
    """手机号 + 密码。

    兼容老账号：account 里如果是带 @ 的邮箱，按邮箱查；
    否则按手机号查，最后再兜一次用户名。
    """
    raw = (data.account or "").strip()
    account = raw.lower()
    ip = client_ip(request)
    if not raw:
        raise HTTPException(400, "请填写手机号")
    if not data.password:
        raise HTTPException(400, "请填写密码")
    if not login_limiter.hit("%s|%s" % (ip, account)):
        raise HTTPException(429, "登录尝试过于频繁，请 5 分钟后再试")

    digits = smscode.normalize(raw)
    row = None
    if _MOBILE_RE.match(digits):
        row = conn.execute("SELECT * FROM users WHERE mobile = ?", (digits,)).fetchone()
    if row is None and "@" in account:
        row = conn.execute("SELECT * FROM users WHERE lower(email) = ?",
                           (account,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM users WHERE lower(username) = ?",
                           (account,)).fetchone()

    if not row or not verify_password(data.password, row["password_hash"]):
        raise HTTPException(401, "账号或密码错误")

    # 老账号第一次用密码登录时，如果填的是手机号而库里没有，不会有这条分支；
    # 这里只处理状态。
    if row["status"] != "active":
        raise HTTPException(403, "账号已被禁用，请联系客服")

    login_limiter.reset("%s|%s" % (ip, account))
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (db.now(), row["id"]))
    days = SESSION_DAYS if data.remember else 1
    token = _issue_session(conn, row["id"], request, response, days)
    db.audit(conn, row["id"], "login", "user", str(row["id"]), ip=ip,
             after={"mode": "password"})
    conn.commit()
    return {"ok": True, "token": token, "user": _profile(conn, row)}


# ------------------------------------------------------------------ 找回密码
@router.post("/reset-password")
def reset_password(data: ResetIn, request: Request, conn=Depends(db.get_db)):
    """手机号 + 短信验证码 → 重设密码。不返回登录态，让用户用新密码登一次。"""
    ip = client_ip(request)
    mobile = _check_mobile(data.mobile)
    if len(data.new_password) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    if not login_limiter.hit("reset|%s|%s" % (ip, mobile)):
        raise HTTPException(429, "尝试过于频繁，请 5 分钟后再试")

    row = conn.execute("SELECT * FROM users WHERE mobile = ?", (mobile,)).fetchone()
    if not row:
        raise HTTPException(404, "该手机号还没有注册，请先注册账号")

    passed, why = smscode.verify(conn, mobile, "reset", data.sms_code)
    if not passed:
        raise HTTPException(400, why)

    if verify_password(data.new_password, row["password_hash"]):
        raise HTTPException(400, "新密码不能和原密码相同")

    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (hash_password(data.new_password), row["id"]))
    # 密码被重置后，所有旧会话立刻失效 —— 假设的场景是「号可能被别人用过」
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
    db.audit(conn, row["id"], "password.reset", "user", str(row["id"]), ip=ip,
             after={"mobile": sms.mask(mobile)})
    conn.commit()
    login_limiter.reset("reset|%s|%s" % (ip, mobile))
    return {"ok": True, "message": "密码已重置，请用新密码登录"}


# ------------------------------------------------------------------ 会话
@router.post("/logout")
def logout(request: Request, response: Response, conn=Depends(db.get_db)):
    from ..deps import _token_from

    tok = _token_from(request)
    if tok:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(tok),))
        conn.commit()
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
def me(user=Depends(current_user), conn=Depends(db.get_db)):
    return {"ok": True, "user": _profile(conn, user)}


@router.post("/password")
def change_password(data: PasswordIn, user=Depends(current_user),
                    conn=Depends(db.get_db)):
    if not verify_password(data.old_password, user["password_hash"]):
        raise HTTPException(400, "原密码不正确")
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (hash_password(data.new_password), user["id"]))
    # 改密后其它会话全部失效
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    conn.commit()
    return {"ok": True, "message": "密码已更新，请重新登录"}


@router.post("/device-token")
def create_device_token(payload: dict, user=Depends(current_user),
                        conn=Depends(db.get_db)):
    """桌面端用：签发长期令牌，避免每次启动都要输密码。"""
    name = str(payload.get("name") or "Windows 客户端")[:60]
    token = new_token(36)
    conn.execute("INSERT INTO device_tokens(token_hash, user_id, name, created_at, last_seen) "
                 "VALUES(?,?,?,?,?)",
                 (token_hash(token), user["id"], name, db.now(), db.now()))
    conn.commit()
    return {"ok": True, "token": token, "expires_in_days": DEVICE_DAYS}
