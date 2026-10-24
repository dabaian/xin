"""素材库：参考图上传、列表、删除。"""
from __future__ import annotations

import hashlib
import os
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from .. import db, signing
from ..config import CFG
from ..deps import current_user

router = APIRouter(prefix="/api/assets", tags=["assets"])

MAX_BYTES = 12 * 1024 * 1024
ALLOWED = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
# 魔数校验，防止改扩展名绕过
MAGIC = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG\r\n\x1a\n": ".png",
    b"RIFF": ".webp",
}


def _sniff(head: bytes) -> str:
    for magic, ext in MAGIC.items():
        if head.startswith(magic):
            if ext == ".webp" and head[8:12] != b"WEBP":
                return ""
            return ext
    return ""


@router.post("/upload")
async def upload(file: UploadFile = File(...), user=Depends(current_user),
                 conn=Depends(db.get_db)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "文件是空的")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "图片不能超过 12MB")
    ext = _sniff(data[:16])
    if not ext:
        raise HTTPException(415, "只支持 JPG / PNG / WEBP 图片")

    name = uuid.uuid4().hex + ext
    path = os.path.join(CFG.media_dir, name)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)

    mime = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[ext]
    cur = conn.execute(
        "INSERT INTO media_assets(user_id, storage_key, kind, mime, bytes, sha256, status, "
        "source, title, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (user["id"], name, "upload", mime, len(data),
         hashlib.sha256(data).hexdigest(), "ready", "upload",
         (file.filename or "参考图")[:60], db.now()))
    conn.commit()
    aid = cur.lastrowid
    return {"ok": True, "asset": {"id": aid, "url": signing.media_url(aid),
                                  "bytes": len(data), "kind": "upload"}}


@router.get("")
def list_assets(kind: str = "", limit: int = 40, offset: int = 0,
                user=Depends(current_user), conn=Depends(db.get_db)):
    limit = max(1, min(limit, 100))
    sql = "SELECT * FROM media_assets WHERE user_id = ? AND status = 'ready'"
    args: list = [user["id"]]
    if kind in ("video", "image", "upload"):
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args.extend([limit, offset])
    rows = conn.execute(sql, tuple(args)).fetchall()
    used = conn.execute(
        "SELECT COALESCE(SUM(bytes),0) b, COUNT(*) c FROM media_assets "
        "WHERE user_id=? AND status='ready'", (user["id"],)).fetchone()
    return {
        "ok": True,
        "items": [{
            "id": r["id"], "kind": r["kind"], "mime": r["mime"], "bytes": r["bytes"],
            "title": r["title"], "created_at": r["created_at"],
            "url": signing.media_url(r["id"]),
        } for r in rows],
        "usage": {"bytes": used["b"], "count": used["c"]},
    }


@router.delete("/{asset_id}")
def delete_asset(asset_id: int, user=Depends(current_user), conn=Depends(db.get_db)):
    row = conn.execute("SELECT * FROM media_assets WHERE id=? AND user_id=?",
                       (asset_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "素材不存在")
    # 有任务在跑时先别删参考图：上游可能还没把它取走
    if row["kind"] == "upload":
        busy = conn.execute(
            "SELECT COUNT(*) c FROM generation_tasks WHERE user_id=? AND status IN "
            "('QUEUED','SUBMITTING','SUBMITTED','RUNNING')",
            (user["id"],)).fetchone()["c"]
        if busy:
            raise HTTPException(409, "你有任务还在生成，等它完成后再删参考图")
    conn.execute("DELETE FROM media_assets WHERE id=?", (asset_id,))
    conn.commit()
    try:
        os.remove(os.path.join(CFG.media_dir, row["storage_key"]))
    except OSError:
        pass
    return {"ok": True}
