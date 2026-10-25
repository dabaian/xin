"""短信验证码：签发、校验、限速。

规则（比邮箱更严一点，因为短信按条计费，被刷就是直接烧钱）：
 - 6 位数字，默认 5 分钟有效；
 - 同一手机号同一用途 60 秒内只能发一次，每小时最多 5 条；
 - 同一 IP 每小时最多 20 条（防止换号轰炸）；
 - 一张码最多试错 5 次，超过即作废；
 - 校验成功立刻标记 used，不能重复用。

只存 `sha256(code)`，库里没有明文验证码 —— 拖库也拿不到能直接用的码。
"""
from __future__ import annotations

import math
import random

from . import db
from .config import CFG
from .security import token_hash

CODE_LEN = 6
COOLDOWN = 60          # 同手机号两次发码间隔（秒）
PER_HOUR = 5           # 同手机号每小时上限
IP_PER_HOUR = 20       # 同 IP 每小时上限
MAX_ATTEMPTS = 5       # 单张码最多试错次数
PURPOSES = ("register", "reset", "login")


def normalize(mobile: str) -> str:
    """去掉空格 / 横线 / +86 前缀，只留 11 位数字。"""
    s = "".join(ch for ch in (mobile or "") if ch.isdigit())
    if len(s) == 13 and s.startswith("86"):
        s = s[2:]
    return s


def issue(conn, mobile: str, purpose: str, ip: str = "") -> tuple[str, str]:
    """签发一张验证码。返回 (明文码, 错误信息)；出错时明文码为空串。"""
    mobile = normalize(mobile)
    if purpose not in PURPOSES:
        return "", "验证码用途不合法"

    now = db.now()
    row = conn.execute(
        "SELECT (julianday(?) - julianday(created_at)) * 86400.0 AS gap "
        "FROM sms_codes WHERE mobile=? AND purpose=? ORDER BY id DESC LIMIT 1",
        (now, mobile, purpose)).fetchone()
    if row and row["gap"] is not None and row["gap"] < COOLDOWN:
        left = int(math.ceil(COOLDOWN - float(row["gap"])))
        return "", "发送太频繁，请 %d 秒后再试" % max(1, left)

    cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM sms_codes WHERE mobile=? AND purpose=? "
        "AND (julianday(?) - julianday(created_at)) * 86400.0 < 3600",
        (mobile, purpose, now)).fetchone()["c"]
    if int(cnt) >= PER_HOUR:
        return "", "该手机号一小时内最多获取 %d 次验证码，请稍后再试" % PER_HOUR

    if ip:
        ipcnt = conn.execute(
            "SELECT COUNT(*) AS c FROM sms_codes WHERE ip=? "
            "AND (julianday(?) - julianday(created_at)) * 86400.0 < 3600",
            (ip, now)).fetchone()["c"]
        if int(ipcnt) >= IP_PER_HOUR:
            return "", "当前网络获取验证码次数过多，请稍后再试"

    code = "".join(random.choice("0135792468") for _ in range(CODE_LEN))
    conn.execute(
        "INSERT INTO sms_codes(mobile, purpose, code_hash, ip, created_at, expires_at) "
        "VALUES(?,?,?,?,?,?)",
        (mobile, purpose, token_hash(code), ip or "", now, _expires()))
    conn.commit()
    return code, ""


def verify(conn, mobile: str, purpose: str, code: str) -> tuple[bool, str]:
    """校验并消费一张验证码。返回 (是否通过, 失败原因)。"""
    mobile = normalize(mobile)
    code = (code or "").strip()
    if not code:
        return False, "请填写短信验证码"

    row = conn.execute(
        "SELECT * FROM sms_codes WHERE mobile=? AND purpose=? AND used=0 "
        "AND (julianday(?) - julianday(expires_at)) * 86400.0 < 0 "
        "ORDER BY id DESC LIMIT 1", (mobile, purpose, db.now())).fetchone()
    if not row:
        return False, "验证码已失效，请重新获取"

    if int(row["attempts"] or 0) >= MAX_ATTEMPTS:
        conn.execute("UPDATE sms_codes SET used=1 WHERE id=?", (row["id"],))
        conn.commit()
        return False, "验证码错误次数过多，请重新获取"

    if row["code_hash"] != token_hash(code):
        conn.execute("UPDATE sms_codes SET attempts = attempts + 1 WHERE id=?", (row["id"],))
        conn.commit()
        left = MAX_ATTEMPTS - int(row["attempts"] or 0) - 1
        return False, ("验证码不正确，还可以再试 %d 次" % left) if left > 0 else "验证码不正确"

    conn.execute("UPDATE sms_codes SET used=1 WHERE id=?", (row["id"],))
    conn.commit()
    return True, ""


def purge(conn) -> int:
    cur = conn.execute(
        "DELETE FROM sms_codes WHERE (julianday(?) - julianday(expires_at)) * 86400.0 > 86400",
        (db.now(),))
    conn.commit()
    return cur.rowcount


def ttl_minutes() -> int:
    return max(1, int(math.ceil(int(CFG.sms_code_ttl) / 60)))


def _expires() -> str:
    return db.now_plus(ttl_minutes())
