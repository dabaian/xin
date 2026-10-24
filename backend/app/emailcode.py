"""邮箱验证码：签发、校验、限速。

规则：
 - 6 位数字，默认 10 分钟有效；
 - 同一邮箱同一用途 60 秒内只能发一次，每小时最多 5 次；
 - 一张码最多试错 5 次，超过即作废；
 - 校验成功立刻标记 used，不能重复用。
"""
from __future__ import annotations

import math
import random

from . import db
from .config import CFG
from .security import token_hash

CODE_LEN = 6
COOLDOWN = 60        # 同邮箱两次发信的间隔（秒）
PER_HOUR = 5         # 同邮箱每小时上限
MAX_ATTEMPTS = 5     # 单张码最多试错次数
PURPOSES = ("register", "login")


def normalize(email: str) -> str:
    return (email or "").strip().lower()


def issue(conn, email: str, purpose: str, ip: str = "") -> tuple[str, str]:
    """签发一张验证码。返回 (明文码, 错误信息)；出错时明文码为空串。"""
    email = normalize(email)
    if purpose not in PURPOSES:
        return "", "验证码用途不合法"

    now = db.now()
    row = conn.execute(
        "SELECT (julianday(?) - julianday(created_at)) * 86400.0 AS gap "
        "FROM email_codes WHERE email=? AND purpose=? ORDER BY id DESC LIMIT 1",
        (now, email, purpose)).fetchone()
    if row and row["gap"] is not None and row["gap"] < COOLDOWN:
        left = int(math.ceil(COOLDOWN - float(row["gap"])))
        return "", "发送太频繁，请 %d 秒后再试" % max(1, left)

    cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM email_codes WHERE email=? AND purpose=? "
        "AND (julianday(?) - julianday(created_at)) * 86400.0 < 3600",
        (email, purpose, now)).fetchone()["c"]
    if int(cnt) >= PER_HOUR:
        return "", "该邮箱一小时内最多获取 %d 次验证码，请稍后再试" % PER_HOUR

    code = "".join(random.choice("0123456789") for _ in range(CODE_LEN))
    conn.execute(
        "INSERT INTO email_codes(email, purpose, code_hash, ip, created_at, expires_at) "
        "VALUES(?,?,?,?,?,?)",
        (email, purpose, token_hash(code), ip or "", now, _expires()))
    conn.commit()
    return code, ""


def verify(conn, email: str, purpose: str, code: str) -> tuple[bool, str]:
    """校验并消费一张验证码。返回 (是否通过, 失败原因)。"""
    email = normalize(email)
    code = (code or "").strip()
    if not code:
        return False, "请填写邮箱验证码"

    row = conn.execute(
        "SELECT * FROM email_codes WHERE email=? AND purpose=? AND used=0 "
        "AND (julianday(?) - julianday(expires_at)) * 86400.0 < 0 "
        "ORDER BY id DESC LIMIT 1", (email, purpose, db.now())).fetchone()
    if not row:
        return False, "验证码已失效，请重新获取"

    if int(row["attempts"] or 0) >= MAX_ATTEMPTS:
        conn.execute("UPDATE email_codes SET used=1 WHERE id=?", (row["id"],))
        conn.commit()
        return False, "验证码错误次数过多，请重新获取"

    if row["code_hash"] != token_hash(code):
        conn.execute("UPDATE email_codes SET attempts = attempts + 1 WHERE id=?", (row["id"],))
        conn.commit()
        left = MAX_ATTEMPTS - int(row["attempts"] or 0) - 1
        return False, ("验证码不正确，还可以再试 %d 次" % left) if left > 0 else "验证码不正确"

    conn.execute("UPDATE email_codes SET used=1 WHERE id=?", (row["id"],))
    conn.commit()
    return True, ""


def purge(conn) -> int:
    cur = conn.execute(
        "DELETE FROM email_codes WHERE (julianday(?) - julianday(expires_at)) * 86400.0 > 86400",
        (db.now(),))
    conn.commit()
    return cur.rowcount


def _expires() -> str:
    return db.now_plus(max(1, math.ceil(int(CFG.email_code_ttl) / 60)))
