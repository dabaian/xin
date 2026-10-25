"""桌面端（EXE）：版本检查、安装包分发、启动引导。"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from ..config import CFG
from ..deps import optional_user

router = APIRouter(prefix="/api/desktop", tags=["desktop"])


def _setup_path() -> str:
    return os.path.join(CFG.release_dir, CFG.desktop_setup_name)


@router.get("/version")
def version():
    path = _setup_path()
    return {
        "ok": True,
        "version": CFG.desktop_version,
        "url": "/api/desktop/download",
        "size": os.path.getsize(path) if os.path.isfile(path) else 0,
        "available": os.path.isfile(path),
        "notes": "新版客户端：全新 Studio 界面、断点续传下载、自动升级",
    }


@router.get("/download")
def download():
    path = _setup_path()
    if not os.path.isfile(path):
        raise HTTPException(404, "安装包还没上传到服务器 releases/ 目录")
    return FileResponse(path, media_type="application/octet-stream",
                        filename=CFG.desktop_setup_name,
                        headers={"X-App-Version": CFG.desktop_version})


@router.get("/bootstrap")
def bootstrap(user=Depends(optional_user)):
    """EXE 启动时读这个接口：拿到站名、版本、是否已登录。"""
    return {
        "ok": True,
        "app_name": CFG.app_name,
        "version": CFG.desktop_version,
        "home": "/studio",
        "logged_in": bool(user),
        "user": ({"mobile": (user["mobile"] or ""),
                  "email": user["email"],
                  "username": user["username"]} if user else None),
    }


@router.post("/report")
def report(payload: dict, user=Depends(optional_user)):
    """客户端上报启动/下载事件，便于排查员工环境问题。"""
    from .. import db

    conn = db.connect()
    try:
        db.audit(conn, user["id"] if user else None,
                 "desktop.%s" % str(payload.get("event") or "report")[:40],
                 "desktop", str(payload.get("version") or ""),
                 after={k: str(v)[:200] for k, v in list(payload.items())[:10]})
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}
