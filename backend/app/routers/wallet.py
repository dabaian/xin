"""钱包：余额、流水、充值套餐。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db, ledger
from ..config import (BONUS_TIERS, CFG, CUSTOM_MAX_YUAN, CUSTOM_MIN_YUAN,
                      custom_package)
from ..deps import current_user, optional_user

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


@router.get("")
def wallet(user=Depends(current_user), conn=Depends(db.get_db)):
    bal = ledger.balance(conn, user["id"])
    consumed = conn.execute(
        "SELECT COALESCE(SUM(-delta_frozen),0) s FROM ledger_entries "
        "WHERE user_id=? AND kind='consume'", (user["id"],)).fetchone()["s"]
    tasks = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(settled_points),0) p FROM generation_tasks "
        "WHERE user_id=? AND status='SUCCEEDED'", (user["id"],)).fetchone()
    return {
        "ok": True,
        "wallet": bal,
        "stats": {"spent_points": consumed, "videos": tasks["c"], "points_used": tasks["p"]},
    }


@router.get("/ledger")
def ledger_list(limit: int = 30, offset: int = 0, user=Depends(current_user),
                conn=Depends(db.get_db)):
    limit = max(1, min(limit, 100))
    items = ledger.ledger_page(conn, user["id"], limit, offset)
    total = conn.execute("SELECT COUNT(*) c FROM ledger_entries WHERE user_id=?",
                         (user["id"],)).fetchone()["c"]
    label = {"recharge": "充值", "consume": "生成消耗", "refund": "失败退回",
             "freeze": "任务冻结", "unfreeze": "解冻", "grant": "赠送", "adjust": "管理员调整"}
    for it in items:
        it["label"] = label.get(it["kind"], it["kind"])
        it["amount"] = it["delta_available"] if it["delta_available"] else it["delta_frozen"]
    return {"ok": True, "items": items, "total": total}


@router.get("/packages")
def packages(user=Depends(optional_user)):
    items = []
    for p in CFG.packages:
        try:
            points = int(p.get("points") or 0)
            price = int(p.get("price") or 0)
        except (TypeError, ValueError):
            continue
        bonus = int(p.get("bonus") or 0)
        items.append({
            "id": p.get("id") or "pkg",
            "name": p.get("name") or "充值包",
            "points": points,
            "bonus": bonus,
            "total_points": points + bonus,
            "price_minor": price,
            "price_yuan": "%.2f" % (price / 100.0),
            "unit": "%.3f" % (price / 100.0 / max(1, points + bonus)),
            "tag": p.get("tag") or "",
        })
    from ..payments import gateway

    active = gateway.resolve_code()
    adapter = gateway.get(active)
    return {"ok": True, "items": items,
            "alipay_ready": CFG.alipay_ready,
            "mock": gateway.mock_enabled(),
            "channel": {
                "code": active,
                "name": adapter.name if adapter else "",
                "mock": bool(adapter and adapter.is_mock()),
                "ready": bool(adapter and adapter.ready()),
            },
            "channels": gateway.available(),
            "custom": {"min": CUSTOM_MIN_YUAN, "max": CUSTOM_MAX_YUAN,
                       "tiers": [{"label": t["label"],
                                  "min_yuan": t["min_minor"] // 100,
                                  "rate": round(100 * t["num"] / t["den"])}
                                 for t in BONUS_TIERS]}}


@router.get("/custom-quote")
def custom_quote(yuan: float = 0, user=Depends(optional_user)):
    """自定义金额试算：给前端实时显示「到账多少积分、送多少」。"""
    try:
        pkg = custom_package(yuan)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "min": CUSTOM_MIN_YUAN,
                "max": CUSTOM_MAX_YUAN}
    return {"ok": True, "yuan": pkg["price_yuan"], "points": pkg["points"],
            "bonus": pkg["bonus"], "total": pkg["total_points"],
            "bonus_label": pkg["bonus_label"]}
