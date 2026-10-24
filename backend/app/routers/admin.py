"""运营后台：指标、用户、订单、任务、供应商、价格、审计。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import db, ledger, signing, tasks as engine
from ..config import CFG
from ..deps import admin_only, client_ip, current_user
from ..payments import alipay
from ..providers.muse import provider_from_row
from ..security import hash_password

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/overview")
def overview(user=Depends(admin_only), conn=Depends(db.get_db)):
    def one(sql, args=()):
        r = conn.execute(sql, args).fetchone()
        return list(r)[0] if r else 0

    today = db.now()[:10]
    gross = one("SELECT COALESCE(SUM(amount_minor),0) FROM payment_orders WHERE status='PAID' "
                "AND paid_at LIKE ?", (today + "%",))
    gross_all = one("SELECT COALESCE(SUM(amount_minor),0) FROM payment_orders WHERE status='PAID'")
    users_total = one("SELECT COUNT(*) FROM users")
    users_today = one("SELECT COUNT(*) FROM users WHERE created_at LIKE ?", (today + "%",))
    tasks_today = one("SELECT COUNT(*) FROM generation_tasks WHERE created_at LIKE ?",
                      (today + "%",))
    ok_today = one("SELECT COUNT(*) FROM generation_tasks WHERE status='SUCCEEDED' "
                   "AND created_at LIKE ?", (today + "%",))
    fail_today = one("SELECT COUNT(*) FROM generation_tasks WHERE status='FAILED' "
                     "AND created_at LIKE ?", (today + "%",))
    running = one("SELECT COUNT(*) FROM generation_tasks WHERE status IN "
                  "('QUEUED','SUBMITTING','SUBMITTED','RUNNING')")
    review = one("SELECT COUNT(*) FROM generation_tasks WHERE status='NEEDS_REVIEW'")
    frozen = one("SELECT COALESCE(SUM(frozen_points),0) FROM wallets")
    avail = one("SELECT COALESCE(SUM(available_points),0) FROM wallets")
    consumed_today = one(
        "SELECT COALESCE(SUM(-delta_frozen),0) FROM ledger_entries WHERE kind='consume' "
        "AND created_at LIKE ?", (today + "%",))

    # 近 7 天趋势
    trend = []
    for i in range(6, -1, -1):
        from datetime import datetime, timedelta

        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        trend.append({
            "date": d[5:],
            "orders": one("SELECT COUNT(*) FROM payment_orders WHERE status='PAID' "
                          "AND paid_at LIKE ?", (d + "%",)),
            "amount": one("SELECT COALESCE(SUM(amount_minor),0) FROM payment_orders "
                          "WHERE status='PAID' AND paid_at LIKE ?", (d + "%",)) / 100.0,
            "tasks": one("SELECT COUNT(*) FROM generation_tasks WHERE created_at LIKE ?",
                         (d + "%",)),
        })

    prow = conn.execute("SELECT * FROM model_providers WHERE code='muse'").fetchone()
    health = {"ok": False}
    if prow:
        try:
            health = provider_from_row(prow).health()
        except Exception as exc:  # noqa: BLE001
            health = {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "kpi": {
            "gross_today_minor": gross, "gross_all_minor": gross_all,
            "users_total": users_total, "users_today": users_today,
            "tasks_today": tasks_today, "ok_today": ok_today, "fail_today": fail_today,
            "running": running, "needs_review": review,
            "frozen_points": frozen, "available_points": avail,
            "consumed_points_today": consumed_today,
            "success_rate": round(ok_today / tasks_today * 100, 1) if tasks_today else None,
        },
        "trend": trend,
        "provider": {"code": "muse", "enabled": bool(prow and prow["enabled"]),
                     "base_url": prow["base_url"] if prow else "",
                     "health": health},
        "alipay": alipay.selfcheck(),
    }


@router.get("/users")
def users(keyword: str = "", limit: int = 30, offset: int = 0,
          user=Depends(admin_only), conn=Depends(db.get_db)):
    sql = ("SELECT u.id, u.email, u.username, u.role, u.status, u.created_at, u.last_login_at, "
           "w.available_points, w.frozen_points, w.total_recharged FROM users u "
           "LEFT JOIN wallets w ON w.user_id = u.id WHERE 1=1")
    args: list = []
    if keyword:
        sql += " AND (u.email LIKE ? OR u.username LIKE ?)"
        args += ["%" + keyword + "%", "%" + keyword + "%"]
    sql += " ORDER BY u.id DESC LIMIT ? OFFSET ?"
    args += [max(1, min(limit, 100)), offset]
    rows = conn.execute(sql, tuple(args)).fetchall()
    return {"ok": True, "items": [dict(r) for r in rows]}


@router.patch("/users/{uid}")
def update_user(uid: int, payload: dict, request: Request, admin=Depends(admin_only),
                conn=Depends(db.get_db)):
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        raise HTTPException(404, "用户不存在")
    before = {"status": row["status"], "role": row["role"]}
    if "status" in payload:
        st = str(payload["status"])
        if st not in ("active", "disabled"):
            raise HTTPException(400, "状态只能是 active / disabled")
        if row["id"] == admin["id"] and st == "disabled":
            raise HTTPException(400, "不能禁用自己")
        conn.execute("UPDATE users SET status=? WHERE id=?", (st, uid))
    if "role" in payload:
        role = str(payload["role"])
        if role not in ("user", "admin"):
            raise HTTPException(400, "角色只能是 user / admin")
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    if payload.get("reset_password"):
        pwd = str(payload["reset_password"])
        if len(pwd) < 6:
            raise HTTPException(400, "新密码至少 6 位")
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(pwd), uid))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    db.audit(conn, admin["id"], "admin.user.update", "user", str(uid), before,
             {"status": payload.get("status"), "role": payload.get("role")},
             ip=client_ip(request))
    conn.commit()
    return {"ok": True}


@router.post("/users/{uid}/points")
def adjust_points(uid: int, payload: dict, request: Request, admin=Depends(admin_only),
                  conn=Depends(db.get_db)):
    try:
        delta = int(payload.get("delta") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "积分必须是整数") from None
    if delta == 0 or abs(delta) > 100000:
        raise HTTPException(400, "调整幅度不合法")
    memo = str(payload.get("memo") or "管理员调整")[:120]
    try:
        bal = ledger.adjust(conn, uid, delta, admin["id"], memo)
    except ledger.LedgerError as exc:
        conn.rollback()
        raise HTTPException(400, exc.message) from exc
    db.audit(conn, admin["id"], "admin.points", "user", str(uid),
             after={"delta": delta, "memo": memo, "balance": bal},
             ip=client_ip(request))
    conn.commit()
    return {"ok": True, "wallet": bal}


@router.get("/orders")
def orders(status: str = "", limit: int = 30, offset: int = 0,
           user=Depends(admin_only), conn=Depends(db.get_db)):
    sql = ("SELECT o.*, u.email FROM payment_orders o LEFT JOIN users u ON u.id=o.user_id "
           "WHERE 1=1")
    args: list = []
    if status:
        sql += " AND o.status=?"
        args.append(status)
    sql += " ORDER BY o.id DESC LIMIT ? OFFSET ?"
    args += [max(1, min(limit, 100)), offset]
    return {"ok": True, "items": [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]}


@router.get("/tasks")
def all_tasks(status: str = "", keyword: str = "", limit: int = 30, offset: int = 0,
              user=Depends(admin_only), conn=Depends(db.get_db)):
    sql = ("SELECT t.*, u.email FROM generation_tasks t LEFT JOIN users u ON u.id=t.user_id "
           "WHERE 1=1")
    args: list = []
    if status:
        sql += " AND t.status IN (%s)" % ",".join("?" * len(status.split(",")))
        args += status.split(",")
    if keyword:
        sql += " AND (t.prompt LIKE ? OR t.task_uid LIKE ?)"
        args += ["%" + keyword + "%", "%" + keyword + "%"]
    sql += " ORDER BY t.id DESC LIMIT ? OFFSET ?"
    args += [max(1, min(limit, 100)), offset]
    rows = conn.execute(sql, tuple(args)).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d.pop("final_prompt", None)
        d["media_url"] = signing.media_url(r["result_asset_id"]) if r["result_asset_id"] else ""
        items.append(d)
    return {"ok": True, "items": items}


@router.post("/tasks/{task_uid}/retry")
def retry_task(task_uid: str, request: Request, admin=Depends(admin_only),
               conn=Depends(db.get_db)):
    row = conn.execute("SELECT * FROM generation_tasks WHERE task_uid=?", (task_uid,)).fetchone()
    if not row:
        raise HTTPException(404, "任务不存在")
    if row["status"] not in ("FAILED", "NEEDS_REVIEW", "CANCELLED"):
        raise HTTPException(400, "只有失败 / 待对账 / 已取消的任务可以重试")
    # 已经解冻过的重新冻结（幂等：余额不足直接拒绝）
    try:
        ledger.freeze(conn, row["user_id"], row["reserved_points"], "task", task_uid, "管理员重试")
    except ledger.LedgerError as exc:
        conn.rollback()
        raise HTTPException(400, "用户余额不足，无法重试：%s" % exc.message) from exc
    conn.execute("UPDATE generation_tasks SET status='QUEUED', provider_task_id='', "
                 "progress=0, error_code='', error_message='', started_at='', finished_at='' "
                 "WHERE id=?", (row["id"],))
    db.audit(conn, admin["id"], "admin.task.retry", "task", task_uid, ip=client_ip(request))
    conn.commit()
    return {"ok": True}


@router.post("/tasks/{task_uid}/resolve")
def resolve_task(task_uid: str, payload: dict, request: Request, admin=Depends(admin_only),
                 conn=Depends(db.get_db)):
    """人工裁定 NEEDS_REVIEW：退款 or 视为已消费。"""
    row = conn.execute("SELECT * FROM generation_tasks WHERE task_uid=?", (task_uid,)).fetchone()
    if not row:
        raise HTTPException(404, "任务不存在")
    action = str(payload.get("action") or "")
    if action not in ("refund", "charge"):
        raise HTTPException(400, "action 只能是 refund / charge")
    if action == "refund":
        ledger.release(conn, row["user_id"], row["reserved_points"], "task", task_uid,
                       "管理员裁定退款")
        conn.execute("UPDATE generation_tasks SET status='FAILED', error_message=? WHERE id=?",
                     ("人工裁定：退款", row["id"]))
    else:
        ledger.settle(conn, row["user_id"], row["reserved_points"], "task", task_uid,
                      "管理员裁定已消费")
        conn.execute("UPDATE generation_tasks SET status='FAILED', settled_points=?, "
                     "error_message=? WHERE id=?",
                     (row["reserved_points"], "人工裁定：已消费", row["id"]))
    db.audit(conn, admin["id"], "admin.task.resolve", "task", task_uid,
             after={"action": action}, ip=client_ip(request))
    conn.commit()
    return {"ok": True}


@router.get("/providers")
def providers(user=Depends(admin_only), conn=Depends(db.get_db)):
    rows = conn.execute("SELECT * FROM model_providers ORDER BY priority").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["api_key"] = "已配置" if r["api_key"] else ""
        try:
            d["capabilities"] = json.loads(r["capabilities_json"] or "{}")
        except json.JSONDecodeError:
            d["capabilities"] = {}
        d.pop("capabilities_json", None)
        out.append(d)
    return {"ok": True, "items": out}


@router.patch("/providers/{code}")
def update_provider(code: str, payload: dict, request: Request,
                    admin=Depends(admin_only), conn=Depends(db.get_db)):
    row = conn.execute("SELECT * FROM model_providers WHERE code=?", (code,)).fetchone()
    if not row:
        raise HTTPException(404, "供应商不存在")
    before = {"enabled": row["enabled"], "base_url": row["base_url"]}
    sets, args = [], []
    for field_name in ("base_url", "note", "api_key", "name"):
        if field_name in payload and payload[field_name] is not None:
            sets.append("%s=?" % field_name)
            args.append(str(payload[field_name]).strip())
    if "enabled" in payload:
        sets.append("enabled=?")
        args.append(1 if payload["enabled"] else 0)
    if "capabilities" in payload and isinstance(payload["capabilities"], dict):
        sets.append("capabilities_json=?")
        args.append(json.dumps(payload["capabilities"], ensure_ascii=False))
    if not sets:
        raise HTTPException(400, "没有要更新的字段")
    args.append(code)
    conn.execute("UPDATE model_providers SET %s WHERE code=?" % ",".join(sets), tuple(args))
    db.audit(conn, admin["id"], "admin.provider.update", "provider", code, before,
             {k: payload.get(k) for k in ("enabled", "base_url") if k in payload},
             ip=client_ip(request))
    conn.commit()
    return {"ok": True}


@router.post("/providers/{code}/test")
def test_provider(code: str, admin=Depends(admin_only), conn=Depends(db.get_db)):
    row = conn.execute("SELECT * FROM model_providers WHERE code=?", (code,)).fetchone()
    if not row:
        raise HTTPException(404, "供应商不存在")
    p = provider_from_row(row)
    return {"ok": True, "health": p.health(), "capabilities": p.capabilities()}


@router.get("/pricing")
def pricing(user=Depends(admin_only), conn=Depends(db.get_db)):
    rows = conn.execute("SELECT * FROM pricing_rules WHERE active=1 "
                        "ORDER BY duration, resolution").fetchall()
    return {"ok": True, "items": [dict(r) for r in rows]}


@router.post("/pricing")
def upsert_pricing(payload: dict, request: Request, admin=Depends(admin_only),
                   conn=Depends(db.get_db)):
    try:
        duration = int(payload.get("duration") or 0)
        points = int(payload.get("points") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "时长和积分必须是整数") from None
    if duration <= 0 or points <= 0:
        raise HTTPException(400, "时长和积分必须大于 0")
    resolution = str(payload.get("resolution") or "720p")
    conn.execute("UPDATE pricing_rules SET active=0 WHERE provider_code='muse' AND "
                 "model='muse-video' AND duration=? AND resolution=?", (duration, resolution))
    conn.execute("INSERT INTO pricing_rules(provider_code, model, duration, resolution, "
                 "points, effective_at, active) VALUES('muse','muse-video',?,?,?,?,1)",
                 (duration, resolution, points, db.now()))
    db.audit(conn, admin["id"], "admin.pricing", "pricing", "%s/%s" % (duration, resolution),
             after={"points": points}, ip=client_ip(request))
    conn.commit()
    return {"ok": True}


@router.get("/audit")
def audit_logs(limit: int = 50, offset: int = 0, user=Depends(admin_only),
               conn=Depends(db.get_db)):
    rows = conn.execute(
        "SELECT a.*, u.email FROM audit_logs a LEFT JOIN users u ON u.id=a.actor_id "
        "ORDER BY a.id DESC LIMIT ? OFFSET ?",
        (max(1, min(limit, 200)), offset)).fetchall()
    return {"ok": True, "items": [dict(r) for r in rows]}


@router.get("/health")
def health(user=Depends(current_user), conn=Depends(db.get_db)):
    """普通用户也能看到的服务健康（不暴露密钥）。"""
    prow = conn.execute("SELECT * FROM model_providers WHERE code='muse'").fetchone()
    pending = conn.execute(
        "SELECT COUNT(*) c FROM generation_tasks WHERE status IN "
        "('QUEUED','SUBMITTING','SUBMITTED','RUNNING')").fetchone()["c"]
    return {"ok": True, "queue": pending, "provider_enabled": bool(prow and prow["enabled"]),
            "alipay_ready": CFG.alipay_ready, "app": CFG.app_name,
            "desktop_version": CFG.desktop_version}
