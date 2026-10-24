"""积分账本：只追加流水，余额不允许为负。

约定（对齐 V3 文档 §7）：
  · 提交任务 → freeze()  可用 -N，冻结 +N
  · 生成成功 → settle()  冻结 -N（真实消耗）
  · 最终失败 → release() 冻结 -N，可用 +N
  · 充值     → recharge() 可用 +N
所有函数都复用调用方的事务，不自行 commit。
"""
from __future__ import annotations

import sqlite3

from .db import now


class LedgerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def ensure_wallet(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
    if row:
        return row
    conn.execute("INSERT OR IGNORE INTO wallets(user_id, updated_at) VALUES(?,?)",
                 (user_id, now()))
    return conn.execute("SELECT * FROM wallets WHERE user_id = ?", (user_id,)).fetchone()


def _wallet(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        row = ensure_wallet(conn, user_id)
    return row


def _log(conn, wallet: sqlite3.Row, user_id: int, kind: str, da: int, df: int,
         ref_type: str, ref_id: str, memo: str) -> None:
    conn.execute(
        "INSERT INTO ledger_entries(wallet_id, user_id, kind, delta_available, delta_frozen, "
        "balance_after, frozen_after, ref_type, ref_id, memo, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (wallet["id"], user_id, kind, da, df,
         wallet["available_points"], wallet["frozen_points"], ref_type, ref_id, memo, now()))


def balance(conn: sqlite3.Connection, user_id: int) -> dict:
    w = _wallet(conn, user_id)
    return {
        "available": w["available_points"],
        "frozen": w["frozen_points"],
        "total_recharged": w["total_recharged"],
    }


def recharge(conn: sqlite3.Connection, user_id: int, points: int, ref_type: str,
             ref_id: str, memo: str = "", count_recharge: bool = True) -> dict:
    """充值 / 赠送。幂等性由调用方（payment_events 唯一约束）保证。"""
    if points <= 0:
        raise LedgerError("BAD_AMOUNT", "积分必须大于 0")
    _wallet(conn, user_id)
    conn.execute(
        "UPDATE wallets SET available_points = available_points + ?, "
        "total_recharged = total_recharged + ?, version = version + 1, updated_at = ? "
        "WHERE user_id = ?",
        (points, points if count_recharge else 0, now(), user_id))
    w = _wallet(conn, user_id)
    _log(conn, w, user_id, "recharge", points, 0, ref_type, ref_id, memo)
    return {"available": w["available_points"], "frozen": w["frozen_points"]}


def freeze(conn: sqlite3.Connection, user_id: int, points: int, ref_type: str,
           ref_id: str, memo: str = "") -> dict:
    """冻结积分。余额不足抛 InsufficientPoints —— 用 UPDATE 的条件表达式做原子守卫。"""
    if points <= 0:
        return balance(conn, user_id)
    _wallet(conn, user_id)
    cur = conn.execute(
        "UPDATE wallets SET available_points = available_points - ?, "
        "frozen_points = frozen_points + ?, version = version + 1, updated_at = ? "
        "WHERE user_id = ? AND available_points >= ?",
        (points, points, now(), user_id, points))
    if cur.rowcount == 0:
        raise LedgerError("INSUFFICIENT_POINTS", "积分不足，请先充值")
    w = _wallet(conn, user_id)
    _log(conn, w, user_id, "freeze", -points, points, ref_type, ref_id, memo)
    return {"available": w["available_points"], "frozen": w["frozen_points"]}


def settle(conn: sqlite3.Connection, user_id: int, points: int, ref_type: str,
           ref_id: str, memo: str = "") -> dict:
    """结算：把冻结的积分真正扣掉。调用方必须保证同一 ref 只结算一次。"""
    if points <= 0:
        return balance(conn, user_id)
    w = _wallet(conn, user_id)
    take = min(points, w["frozen_points"])
    conn.execute(
        "UPDATE wallets SET frozen_points = frozen_points - ?, version = version + 1, "
        "updated_at = ? WHERE user_id = ?", (take, now(), user_id))
    w = _wallet(conn, user_id)
    _log(conn, w, user_id, "consume", 0, -take, ref_type, ref_id, memo)
    return {"available": w["available_points"], "frozen": w["frozen_points"]}


def release(conn: sqlite3.Connection, user_id: int, points: int, ref_type: str,
            ref_id: str, memo: str = "") -> dict:
    """解冻：失败/取消时把冻结的积分退回可用余额。"""
    if points <= 0:
        return balance(conn, user_id)
    w = _wallet(conn, user_id)
    give = min(points, w["frozen_points"])
    if give <= 0:
        return balance(conn, user_id)
    conn.execute(
        "UPDATE wallets SET frozen_points = frozen_points - ?, "
        "available_points = available_points + ?, version = version + 1, updated_at = ? "
        "WHERE user_id = ?", (give, give, now(), user_id))
    w = _wallet(conn, user_id)
    _log(conn, w, user_id, "refund", give, -give, ref_type, ref_id, memo)
    return {"available": w["available_points"], "frozen": w["frozen_points"]}


def adjust(conn: sqlite3.Connection, user_id: int, delta: int, actor_id, memo: str) -> dict:
    """管理员手工调整。负数调整必须保证调整后不为负。"""
    w = _wallet(conn, user_id)
    if delta < 0 and w["available_points"] + delta < 0:
        raise LedgerError("INSUFFICIENT_POINTS", "调整后余额将为负，已拒绝")
    conn.execute("UPDATE wallets SET available_points = available_points + ?, "
                 "version = version + 1, updated_at = ? WHERE user_id = ?",
                 (delta, now(), user_id))
    w = _wallet(conn, user_id)
    _log(conn, w, user_id, "adjust", delta, 0, "admin", str(actor_id or ""), memo)
    return {"available": w["available_points"], "frozen": w["frozen_points"]}


def ledger_page(conn: sqlite3.Connection, user_id: int, limit: int = 30,
                offset: int = 0) -> list[dict]:
    rows = conn.execute(
        "SELECT id, kind, delta_available, delta_frozen, balance_after, ref_type, ref_id, "
        "memo, created_at FROM ledger_entries WHERE user_id = ? ORDER BY id DESC "
        "LIMIT ? OFFSET ?", (user_id, limit, offset)).fetchall()
    return [dict(r) for r in rows]
