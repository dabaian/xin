"""路由公共依赖：登录态、管理员校验、客户端 IP。"""
from __future__ import annotations

import sqlite3

from fastapi import Depends, HTTPException, Request

from . import db
from .security import token_hash

COOKIE_NAME = "msid"


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        return xff.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else ""


def _token_from(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.cookies.get(COOKIE_NAME) or "").strip()


def user_from_token(conn: sqlite3.Connection, token: str):
    if not token:
        return None
    th = token_hash(token)
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ?", (th, db.now())).fetchone()
    if row:
        return row
    # 桌面端长期令牌
    row = conn.execute(
        "SELECT u.* FROM device_tokens d JOIN users u ON u.id = d.user_id "
        "WHERE d.token_hash = ?", (th,)).fetchone()
    if row:
        conn.execute("UPDATE device_tokens SET last_seen = ? WHERE token_hash = ?",
                     (db.now(), th))
        return row
    return None


def current_user(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    user = user_from_token(conn, _token_from(request))
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    if user["status"] != "active":
        raise HTTPException(status_code=403, detail="账号已被禁用，请联系客服")
    return user


def optional_user(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    try:
        return user_from_token(conn, _token_from(request))
    except Exception:  # noqa: BLE001
        return None


def admin_only(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def ok(data=None, **extra):
    payload = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return payload
