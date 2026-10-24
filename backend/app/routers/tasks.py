"""生成任务：报价、提交、查询、取消、成品分发。"""
from __future__ import annotations

import json
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import db, ledger, signing, tasks as engine
from ..config import CFG
from ..deps import client_ip, current_user
from ..providers.muse import provider_from_row

router = APIRouter(prefix="/api", tags=["tasks"])


class TaskIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    kind: str = "video"          # video / image
    duration: int = 10
    aspect_ratio: str = "9:16"
    resolution: str = "720p"
    image_asset_id: int | None = None
    idempotency_key: str = ""
    template_id: int | None = None


# ------------------------------------------------------------------ 能力+价格
@router.get("/models")
def models(user=Depends(current_user), conn=Depends(db.get_db)):
    prow = conn.execute("SELECT * FROM model_providers WHERE code='muse'").fetchone()
    caps = {}
    if prow:
        try:
            caps = json.loads(prow["capabilities_json"] or "{}")
        except json.JSONDecodeError:
            caps = {}
    if not caps:
        from ..providers.muse import DEFAULT_CAPS

        caps = dict(DEFAULT_CAPS)

    rules = conn.execute(
        "SELECT duration, resolution, points FROM pricing_rules WHERE provider_code='muse' "
        "AND model='muse-video' AND active=1 ORDER BY duration").fetchall()
    prices = [dict(r) for r in rules]
    # 补齐缺失时长（2 积分/秒），前端下拉不留空档
    have = {(p["duration"], p["resolution"]) for p in prices}
    for dur in (caps.get("durations") or [10]):
        for res in ("720p",):
            if (dur, res) not in have:
                prices.append({"duration": dur, "resolution": res,
                               "points": max(1, dur * CFG.points_per_second)})
    prices.sort(key=lambda x: (x["duration"], x["resolution"]))

    img_row = conn.execute(
        "SELECT points FROM pricing_rules WHERE provider_code='muse' AND model='muse-image' "
        "AND active=1 ORDER BY id DESC LIMIT 1").fetchone()
    image_points = int(img_row["points"]) if img_row else CFG.points_per_image

    tpls = conn.execute(
        "SELECT id, name, body, category FROM prompt_templates WHERE enabled=1 ORDER BY sort"
    ).fetchall()
    return {
        "ok": True,
        "capabilities": caps,
        "prices": prices,
        "points_per_second": CFG.points_per_second,
        "image_points": image_points,
        "templates": [dict(t) for t in tpls],
        "provider_enabled": bool(prow and prow["enabled"]),
    }


@router.get("/quote")
def get_quote(duration: int = 10, resolution: str = "720p", kind: str = "video",
              user=Depends(current_user), conn=Depends(db.get_db)):
    pts = engine.quote(conn, duration, resolution, kind)
    bal = ledger.balance(conn, user["id"])
    return {"ok": True, "points": pts, "wallet": bal, "affordable": bal["available"] >= pts}


# ------------------------------------------------------------------ 提交
@router.post("/tasks")
def create(payload: TaskIn, request: Request, user=Depends(current_user),
           conn=Depends(db.get_db)):
    prompt = payload.prompt.strip()
    if not prompt:
        raise HTTPException(400, "请先写下画面描述")

    kind = "image" if payload.kind == "image" else "video"
    duration = 0 if kind == "image" else max(3, min(int(payload.duration or 10), 60))
    aspect = payload.aspect_ratio if payload.aspect_ratio in ("9:16", "16:9", "1:1") else (
        "1:1" if kind == "image" else "9:16")
    resolution = payload.resolution if payload.resolution in ("720p", "1080p") else "720p"

    image_url = ""
    if payload.image_asset_id:
        asset = conn.execute(
            "SELECT * FROM media_assets WHERE id=? AND user_id=?",
            (payload.image_asset_id, user["id"])).fetchone()
        if not asset:
            raise HTTPException(404, "参考图不存在或不属于你")
        # 号池要能自己取到这张图：用公网签名链接（PUBLIC_BASE 必须是外网可达地址）
        base = (CFG.site_base or str(request.base_url)).rstrip("/")
        image_url = "%s/api/media/%s?t=%s" % (base, asset["id"], signing.ticket(asset["id"]))

    idem = (payload.idempotency_key or "").strip()[:80]
    if not idem:
        import uuid

        idem = uuid.uuid4().hex

    try:
        task = engine.create_task(conn, user["id"], prompt,
                                  {"duration": duration, "aspect_ratio": aspect,
                                   "resolution": resolution, "image_url": image_url},
                                  idempotency_key=idem, client_ip=client_ip(request),
                                  thread_key="u%d" % user["id"], image_url=image_url,
                                  kind=kind)
    except ledger.LedgerError as exc:
        raise HTTPException(402, exc.message) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    out = engine.public_task(task)
    out["media_url"] = ""
    return {"ok": True, "task": out}


# ------------------------------------------------------------------ 查询
@router.get("/tasks")
def list_tasks(limit: int = 20, offset: int = 0, status: str = "",
               user=Depends(current_user), conn=Depends(db.get_db)):
    limit = max(1, min(limit, 100))
    sql = "SELECT * FROM generation_tasks WHERE user_id = ?"
    args: list = [user["id"]]
    if status:
        sql += " AND status IN (%s)" % ",".join("?" * len(status.split(",")))
        args.extend(status.split(","))
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args.extend([limit, offset])
    rows = conn.execute(sql, tuple(args)).fetchall()
    total = conn.execute("SELECT COUNT(*) c FROM generation_tasks WHERE user_id=?",
                         (user["id"],)).fetchone()["c"]
    items = []
    for r in rows:
        t = engine.public_task(r)
        if r["result_asset_id"]:
            t["media_url"] = signing.media_url(r["result_asset_id"])
            t["thumb"] = ""
        items.append(t)
    return {"ok": True, "items": items, "total": total}


@router.get("/tasks/{task_uid}")
def get_task(task_uid: str, user=Depends(current_user), conn=Depends(db.get_db)):
    row = conn.execute(
        "SELECT * FROM generation_tasks WHERE task_uid = ? AND user_id = ?",
        (task_uid, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "任务不存在")
    out = engine.public_task(row)
    if row["result_asset_id"]:
        out["media_url"] = signing.media_url(row["result_asset_id"])
        asset = conn.execute("SELECT * FROM media_assets WHERE id=?",
                             (row["result_asset_id"],)).fetchone()
        if asset:
            out["bytes"] = asset["bytes"]
    attempts = conn.execute(
        "SELECT attempt_no, status, error_code, started_at FROM generation_attempts "
        "WHERE task_id=? ORDER BY id", (row["id"],)).fetchall()
    out["attempts"] = [dict(a) for a in attempts]
    return {"ok": True, "task": out}


@router.post("/tasks/{task_uid}/cancel")
def cancel(task_uid: str, user=Depends(current_user), conn=Depends(db.get_db)):
    row = conn.execute(
        "SELECT * FROM generation_tasks WHERE task_uid=? AND user_id=?",
        (task_uid, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "任务不存在")
    # 号池不支持取消：只有还在排队、尚未提交的才能取消并退款
    if row["status"] not in ("CREATED", "RESERVED", "QUEUED"):
        raise HTTPException(400, "任务已经被上游受理，无法取消；请等它出片或失败自动退款")
    try:
        ledger.release(conn, user["id"], row["reserved_points"], "task", task_uid, "用户取消")
        conn.execute("UPDATE generation_tasks SET status='CANCELLED', finished_at=?, "
                     "error_message='用户取消' WHERE id=?", (db.now(), row["id"]))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, "取消失败：%s" % exc) from exc
    return {"ok": True, "message": "已取消，积分已退回"}


# ------------------------------------------------------------------ 成品分发
_MEDIA_RE = re.compile(r"^[0-9a-f]{32}\.(mp4|webm|mov|png|jpg|jpeg|webp)$", re.I)


@router.get("/media/{asset_id}")
def serve_media(asset_id: str, t: str = "", conn=Depends(db.get_db)):
    if not signing.verify(asset_id, t):
        raise HTTPException(403, "链接已失效，请刷新页面重新获取")
    asset = conn.execute("SELECT * FROM media_assets WHERE id=?", (asset_id,)).fetchone()
    if not asset:
        raise HTTPException(404, "文件不存在")
    key = asset["storage_key"]
    if not _MEDIA_RE.match(key):
        raise HTTPException(400, "非法文件名")
    path = os.path.join(CFG.media_dir, key)
    if not os.path.isfile(path):
        raise HTTPException(404, "文件已被清理")
    return FileResponse(path, media_type=asset["mime"] or None,
                        filename=key, headers={"Cache-Control": "private, max-age=3600"})
