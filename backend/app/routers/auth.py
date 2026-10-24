"""注册 / 登录 / 会话。"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import db, ledger
from ..config import CFG
from ..deps import COOKIE_NAME, client_ip, current_user
from ..security import (hash_password, login_limiter, new_token, register_limiter,
                        token_hash, verify_password)

router = APIRouter(prefix="/api/auth", tags=["auth"])

SESSION_DAYS = 14
DEVICE_DAYS = 180


class RegisterIn(BaseModel):
    email: str = Field(min_length=5, max_length=120)
    password: str = Field(min_length=6, max_length=64)
    username: str = Field(default="", max_length=32)
    invite: str = ""


class LoginIn(BaseModel):
    account: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=1, max_length=64)
    remember: bool = True


class PasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=64)


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
    return {
        "id": user["id"],
        "email": user["email"],
        "username": user["username"] or user["email"].split("@")[0],
        "role": user["role"],
        "status": user["status"],
        "created_at": user["created_at"],
        "wallet": w,
    }


@router.post("/register")
def register(data: RegisterIn, request: Request, response: Response,
             conn=Depends(db.get_db)):
    if not CFG.allow_register:
        raise HTTPException(403, "当前已关闭注册，请联系客服开通")
    ip = client_ip(request)
    if not register_limiter.hit(ip or "unknown"):
        raise HTTPException(429, "注册太频繁了，请稍后再试")
    email = data.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "邮箱格式不正确")
    if len(data.password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
        raise HTTPException(409, "该邮箱已注册，请直接登录")

    username = (data.username or "").strip() or email.split("@")[0]
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO users(email, username, password_hash, role, status, signup_ip, "
            "created_at) VALUES(?,?,?,'user','active',?,?)",
            (email, username[:32], hash_password(data.password), ip, db.now()))
        uid = cur.lastrowid
        ledger.ensure_wallet(conn, uid)
        if CFG.signup_bonus > 0:
            ledger.recharge(conn, uid, CFG.signup_bonus, "signup", str(uid),
                            "新用户注册赠送", count_recharge=False)
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, "注册失败：%s" % exc) from exc

    user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    token = _issue_session(conn, uid, request, response)
    db.audit(conn, uid, "register", "user", str(uid), ip=ip)
    conn.commit()
    return {"ok": True, "token": token, "user": _profile(conn, user)}


@router.post("/login")
def login(data: LoginIn, request: Request, response: Response,
          conn=Depends(db.get_db)):
    account = data.account.strip().lower()
    ip = client_ip(request)
    if not login_limiter.hit("%s|%s" % (ip, account)):
        raise HTTPException(429, "登录尝试过于频繁，请 5 分钟后再试")

    row = conn.execute(
        "SELECT * FROM users WHERE lower(email) = ? OR lower(username) = ?",
        (account, account)).fetchone()
    if not row or not verify_password(data.password, row["password_hash"]):
        raise HTTPException(401, "账号或密码错误")
    if row["status"] != "active":
        raise HTTPException(403, "账号已被禁用，请联系客服")

    login_limiter.reset("%s|%s" % (ip, account))
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (db.now(), row["id"]))
    days = SESSION_DAYS if data.remember else 1
    token = _issue_session(conn, row["id"], request, response, days)
    db.audit(conn, row["id"], "login", "user", str(row["id"]), ip=ip)
    conn.commit()
    return {"ok": True, "token": token, "user": _profile(conn, row)}


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
