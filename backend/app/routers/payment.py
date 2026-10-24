"""充值：支付宝当面付（扫码）。

安全要点（V3 §7 / §10）：
  · 金额、积分只由服务端按套餐决定，前端只传 package_id
  · 入账只认「验签通过的异步通知」或「主动查单结果为已支付」
  · 幂等靠 payment_events.event_id 唯一约束，重复通知只入账一次
  · 前端"我付好了"永远不作为入账依据
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import string
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .. import db, ledger
from ..config import CFG
from ..deps import client_ip, current_user
from ..payments import alipay, qr

log = logging.getLogger("studio.payment")

router = APIRouter(prefix="/api/payments", tags=["payments"])

ORDER_PREFIX = "MS"


def _order_no() -> str:
    """MS + 年月日时分秒 + 6 位随机 → 28 位以内，符合支付宝 out_trade_no 要求。"""
    return "%s%s%s" % (ORDER_PREFIX, datetime.now().strftime("%Y%m%d%H%M%S"),
                       "".join(random.choices(string.digits, k=6)))


def _parse_timeout(spec: str) -> int:
    m = re.match(r"^(\d+)([mh])$", (spec or "30m").strip())
    if not m:
        return 30
    n, unit = int(m.group(1)), m.group(2)
    return n * 60 if unit == "h" else n


def _pkg(package_id: str) -> dict:
    for p in CFG.packages:
        if str(p.get("id")) == str(package_id):
            try:
                return {
                    "id": p.get("id"),
                    "name": p.get("name") or "充值包",
                    "points": int(p.get("points") or 0),
                    "bonus": int(p.get("bonus") or 0),
                    "price_minor": int(p.get("price") or 0),
                }
            except (TypeError, ValueError):
                break
    raise HTTPException(400, "套餐不存在")


# ------------------------------------------------------------------ 下单
@router.post("/checkout")
def checkout(payload: dict, request: Request, user=Depends(current_user),
             conn=Depends(db.get_db)):
    package_id = str(payload.get("package_id") or "").strip()
    pkg = _pkg(package_id)
    if pkg["price_minor"] <= 0:
        raise HTTPException(400, "套餐价格异常，请联系客服")
    if not CFG.alipay_ready and not CFG.alipay_mock:
        raise HTTPException(503, "充值通道尚未配置（缺少支付宝参数），请联系客服")

    # 复用未支付的同套餐订单，避免用户连点生成一堆二维码
    fresh = conn.execute(
        "SELECT * FROM payment_orders WHERE user_id=? AND package_id=? AND status IN "
        "('CREATED','PENDING') AND expires_at > ? ORDER BY id DESC LIMIT 1",
        (user["id"], package_id, db.now())).fetchone()
    if fresh and fresh["qr_code"]:
        return _checkout_payload(fresh, pkg)

    order_no = _order_no()
    points = pkg["points"] + pkg["bonus"]
    subject = "%s · %s" % (CFG.app_name, pkg["name"])
    minutes = _parse_timeout(CFG.alipay_timeout)
    conn.execute(
        "INSERT INTO payment_orders(order_no, user_id, provider, package_id, package_name, "
        "amount_minor, currency, points, status, channel, subject, created_at, expires_at) "
        "VALUES(?,?,'alipay',?,?,?,'CNY',?,'CREATED','precreate',?,?,?)",
        (order_no, user["id"], package_id, pkg["name"], pkg["price_minor"], points,
         subject, db.now(), db.now_plus(minutes)))
    conn.commit()

    if CFG.alipay_mock:
        # 模拟模式：不调支付宝网关，二维码里放一个可识别的标记
        qr_code = "https://qr.alipay.com/mock/" + order_no
        conn.execute("UPDATE payment_orders SET status='PENDING', qr_code=? WHERE order_no=?",
                     (qr_code, order_no))
        conn.commit()
        row = conn.execute("SELECT * FROM payment_orders WHERE order_no=?",
                           (order_no,)).fetchone()
        out = _checkout_payload(row, pkg)
        out["mock"] = True
        return out

    try:
        qr_code = alipay.precreate(order_no, pkg["price_minor"], subject,
                                   "%dm" % minutes)
    except alipay.AlipayError as exc:
        conn.execute("UPDATE payment_orders SET status='FAILED' WHERE order_no=?", (order_no,))
        db.audit(conn, user["id"], "payment.precreate_failed", "order", order_no,
                 after={"error": exc.message}, ip=client_ip(request))
        conn.commit()
        raise HTTPException(502, "创建支付宝二维码失败：%s" % exc.message) from exc

    conn.execute("UPDATE payment_orders SET status='PENDING', qr_code=? WHERE order_no=?",
                 (qr_code, order_no))
    conn.commit()
    row = conn.execute("SELECT * FROM payment_orders WHERE order_no=?", (order_no,)).fetchone()
    return _checkout_payload(row, pkg)


def _checkout_payload(row, pkg: dict) -> dict:
    qr_text = row["qr_code"] or ""
    return {
        "mock": CFG.alipay_mock,
        "ok": True,
        "order": {
            "order_no": row["order_no"],
            "amount_minor": row["amount_minor"],
            "amount_yuan": "%.2f" % (row["amount_minor"] / 100.0),
            "points": row["points"],
            "package_name": row["package_name"] or pkg["name"],
            "status": row["status"],
            "expires_at": row["expires_at"],
            "qr_text": qr_text,
            "qr_image": qr.data_uri(qr_text, scale=10) if qr_text else "",
        },
    }


# ------------------------------------------------------------------ 查单
@router.get("/orders")
def list_orders(limit: int = 20, offset: int = 0, user=Depends(current_user),
                conn=Depends(db.get_db)):
    rows = conn.execute(
        "SELECT order_no, package_name, amount_minor, points, status, created_at, paid_at "
        "FROM payment_orders WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (user["id"], max(1, min(limit, 100)), offset)).fetchall()
    total = conn.execute("SELECT COUNT(*) c FROM payment_orders WHERE user_id=?",
                         (user["id"],)).fetchone()["c"]
    return {"ok": True, "items": [dict(r) for r in rows], "total": total}


@router.get("/orders/{order_no}")
def order_status(order_no: str, user=Depends(current_user), conn=Depends(db.get_db)):
    row = conn.execute("SELECT * FROM payment_orders WHERE order_no=? AND user_id=?",
                       (order_no, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "订单不存在")
    # 未支付且未过期 → 主动查一次支付宝（通知丢失也能自动到账）
    if row["status"] in ("CREATED", "PENDING") and row["expires_at"] > db.now():
        try:
            info = alipay.query(order_no)
            if _apply_trade(conn, row, info, "query"):
                conn.commit()
                row = conn.execute("SELECT * FROM payment_orders WHERE order_no=?",
                                   (order_no,)).fetchone()
        except alipay.AlipayError as exc:
            log.info("查单失败 %s: %s", order_no, exc.message)
        except Exception as exc:  # noqa: BLE001
            log.warning("查单异常 %s: %s", order_no, exc)
    # 过期处理
    if row["status"] in ("CREATED", "PENDING") and row["expires_at"] <= db.now():
        conn.execute("UPDATE payment_orders SET status='EXPIRED' WHERE order_no=? AND "
                     "status IN ('CREATED','PENDING')", (order_no,))
        conn.commit()
        row = conn.execute("SELECT * FROM payment_orders WHERE order_no=?",
                           (order_no,)).fetchone()

    bal = ledger.balance(conn, user["id"])
    return {
        "ok": True,
        "status": row["status"],
        "paid": row["status"] == "PAID",
        "points": row["points"],
        "wallet": bal,
        "order_no": row["order_no"],
    }


def _apply_trade(conn, order, info: dict, source: str) -> bool:
    """把一次「已支付」事实落库 + 入账，幂等。返回是否刚刚入账。"""
    status = (info.get("trade_status") or "").upper()
    if status not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        return False
    trade_no = str(info.get("trade_no") or "")
    event_id = "alipay:%s:%s" % (trade_no or order["order_no"], status)
    try:
        conn.execute(
            "INSERT INTO payment_events(provider, event_id, order_no, payload_hash, raw, "
            "processed_at) VALUES('alipay',?,?,?,?,?)",
            (event_id, order["order_no"],
             hashlib.sha256(json.dumps(info, ensure_ascii=False, sort_keys=True)
                            .encode()).hexdigest(),
             json.dumps(info, ensure_ascii=False)[:4000], db.now()))
    except Exception:  # noqa: BLE001  —— 唯一约束冲突 = 已处理过
        conn.rollback()
        return False

    # 金额校验：一分钱都不能差
    paid_yuan = str(info.get("total_amount") or "")
    try:
        paid_minor = int(round(float(paid_yuan) * 100))
    except ValueError:
        paid_minor = -1
    if paid_minor != order["amount_minor"] and paid_minor != -1:
        conn.rollback()
        log.error("金额不一致 order=%s 应为 %s 实付 %s", order["order_no"],
                  order["amount_minor"], paid_minor)
        conn.execute(
            "INSERT INTO audit_logs(actor_id, action, target_type, target_id, after_json, "
            "created_at) VALUES(NULL,'payment.amount_mismatch','order',?,?,?)",
            (order["order_no"], json.dumps({"paid": paid_minor, "expect": order["amount_minor"]}),
             db.now()))
        conn.commit()
        return False

    conn.execute("UPDATE payment_orders SET status='PAID', provider_order_id=?, buyer_id=?, "
                 "paid_at=? WHERE order_no=? AND status <> 'PAID'",
                 (trade_no, str(info.get("buyer_id") or ""), db.now(), order["order_no"]))
    ledger.recharge(conn, order["user_id"], order["points"], "order", order["order_no"],
                    "充值到账 · %s" % (order["package_name"] or ""))
    db.audit(conn, order["user_id"], "payment.paid", "order", order["order_no"],
             after={"trade_no": trade_no, "source": source,
                    "points": order["points"]})
    conn.commit()
    log.info("到账 %s 积分 %s (%s)", order["order_no"], order["points"], source)
    return True


# ------------------------------------------------------------------ 异步通知
@router.post("/notify")
async def notify(request: Request, conn=Depends(db.get_db)):
    form = {k: v for k, v in (await request.form()).items()}
    if not form:
        return PlainTextResponse("fail")
    if not alipay.verify_notify(form):
        log.warning("支付宝通知验签失败：%s", form.get("out_trade_no"))
        return PlainTextResponse("fail")
    if CFG.alipay_app_id and form.get("app_id") and form["app_id"] != CFG.alipay_app_id:
        log.warning("app_id 不匹配：%s", form.get("app_id"))
        return PlainTextResponse("fail")

    order_no = form.get("out_trade_no") or ""
    row = conn.execute("SELECT * FROM payment_orders WHERE order_no=?", (order_no,)).fetchone()
    if not row:
        log.warning("通知里的订单不存在：%s", order_no)
        return PlainTextResponse("success")  # 让支付宝别再重试
    info = {
        "trade_status": form.get("trade_status"),
        "trade_no": form.get("trade_no"),
        "total_amount": form.get("total_amount"),
        "buyer_id": form.get("buyer_id") or form.get("buyer_logon_id"),
        "notify_id": form.get("notify_id"),
        "notify_type": form.get("notify_type"),
    }
    _apply_trade(conn, row, info, "notify")
    return PlainTextResponse("success")


@router.post("/mock-pay/{order_no}")
def mock_pay(order_no: str, user=Depends(current_user), conn=Depends(db.get_db)):
    """仅模拟模式可用：把订单标记为已支付，用来验收整条充值链路。"""
    if not CFG.alipay_mock:
        raise HTTPException(404, "接口不存在")
    row = conn.execute("SELECT * FROM payment_orders WHERE order_no=? AND user_id=?",
                       (order_no, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "订单不存在")
    info = {"trade_status": "TRADE_SUCCESS", "trade_no": "MOCK" + order_no,
            "total_amount": "%.2f" % (row["amount_minor"] / 100.0),
            "buyer_id": "mock-user", "notify_id": "mock-" + order_no}
    _apply_trade(conn, row, info, "mock")
    return {"ok": True, "status": "PAID", "points": row["points"]}


# ------------------------------------------------------------------ 查询类
@router.get("/methods")
def methods(user=Depends(current_user)):
    return {
        "ok": True,
        "methods": [{
            "id": "alipay_qr",
            "name": "支付宝扫码",
            "desc": "用支付宝 App 扫一扫，实时到账",
            "enabled": CFG.alipay_ready,
            "icon": "alipay",
        }],
    }


@router.get("/selfcheck")
def selfcheck(user=Depends(current_user)):
    info = alipay.selfcheck()
    info["mock"] = CFG.alipay_mock
    info["notify_url"] = CFG.alipay_notify_url
    info["gateway"] = CFG.alipay_gateway
    return {"ok": True, "alipay": info}


# ------------------------------------------------------------------ 后台清道夫
class PendingSweeper:
    """每 45 秒扫一遍待支付订单，主动查单兜底（通知丢失也能到账）。"""

    def __init__(self):
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def start(self):
        if self._t and self._t.is_alive():
            return
        self._t = threading.Thread(target=self._loop, name="pending-sweeper", daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception as exc:  # noqa: BLE001
                log.warning("对账扫描异常：%s", exc)
            self._stop.wait(45)

    def sweep(self):
        if not CFG.alipay_ready:
            return
        conn = db.connect()
        try:
            # 只查 3 分钟内创建、还没过期的单，避免一上来把历史单全查一遍
            rows = conn.execute(
                "SELECT * FROM payment_orders WHERE status IN ('CREATED','PENDING') "
                "AND created_at > ? AND expires_at > ? ORDER BY id DESC LIMIT 20",
                (db.now()[:11] + "00:00:00", db.now())).fetchall()
            for row in rows:
                try:
                    info = alipay.query(row["order_no"])
                except alipay.AlipayError:
                    continue
                if _apply_trade(conn, row, info, "sweeper"):
                    log.info("扫单补记到账 %s", row["order_no"])
        finally:
            conn.close()
