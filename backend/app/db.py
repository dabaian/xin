"""SQLite 存储层（WAL 模式）。

对齐 V3 架构文档 §5：外键、唯一约束、CHECK（余额不得为负）、
关键流水只追加不覆盖。
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from .config import CFG, RESOLUTIONS, RESOLUTION_VALUES

_LOCK = threading.Lock()
logger = logging.getLogger("muse.db")

# 连接池：SQLite 的 WAL 靠 -shm 文件做共享内存，如果所有连接同时关闭，
# SQLite 会 checkpoint 并删掉 -wal/-shm；此时别的线程正在打开连接，就会拿到
# 只读句柄，写出 `attempt to write a readonly database`（随机 500）。
# 两个对策：① 常驻一个 keepalive 连接，-shm 永不消失；
#          ② 请求用完的连接放回池子复用，不再每次都开关。
_POOL: list[sqlite3.Connection] = []
_POOL_MAX = 12
_KEEPALIVE: sqlite3.Connection | None = None

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    username      TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',        -- user / admin
    status        TEXT NOT NULL DEFAULT 'active',      -- active / disabled / pending
    signup_ip     TEXT NOT NULL DEFAULT '',
    last_login_at TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ua         TEXT NOT NULL DEFAULT '',
    ip         TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS device_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_seen  TEXT NOT NULL DEFAULT ''
);

-- 注册图形验证码：只存答案哈希，一次性使用
CREATE TABLE IF NOT EXISTS captchas (
    id         TEXT PRIMARY KEY,
    code_hash  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_captchas_exp ON captchas(expires_at);

-- 邮箱验证码：注册 / 验证码登录，同样只存哈希
CREATE TABLE IF NOT EXISTS email_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL,
    purpose    TEXT NOT NULL DEFAULT 'register',   -- register / login
    code_hash  TEXT NOT NULL,
    ip         TEXT NOT NULL DEFAULT '',
    attempts   INTEGER NOT NULL DEFAULT 0,
    used       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_codes ON email_codes(email, purpose, created_at);

CREATE TABLE IF NOT EXISTS wallets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    available_points INTEGER NOT NULL DEFAULT 0 CHECK (available_points >= 0),
    frozen_points    INTEGER NOT NULL DEFAULT 0 CHECK (frozen_points >= 0),
    total_recharged  INTEGER NOT NULL DEFAULT 0,
    version          INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id       INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL,
    kind            TEXT NOT NULL,        -- recharge / consume / refund / freeze / unfreeze / grant / adjust
    delta_available INTEGER NOT NULL DEFAULT 0,
    delta_frozen    INTEGER NOT NULL DEFAULT 0,
    balance_after   INTEGER NOT NULL DEFAULT 0,
    frozen_after    INTEGER NOT NULL DEFAULT 0,
    ref_type        TEXT NOT NULL DEFAULT '',
    ref_id          TEXT NOT NULL DEFAULT '',
    memo            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger_entries(user_id, id DESC);

CREATE TABLE IF NOT EXISTS payment_orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no          TEXT NOT NULL UNIQUE,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider          TEXT NOT NULL DEFAULT 'alipay',
    provider_order_id TEXT NOT NULL DEFAULT '',      -- 支付宝 trade_no
    package_id        TEXT NOT NULL DEFAULT '',
    package_name      TEXT NOT NULL DEFAULT '',
    amount_minor      INTEGER NOT NULL,              -- 单位：分
    currency          TEXT NOT NULL DEFAULT 'CNY',
    points            INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'CREATED', -- CREATED/PENDING/PAID/FAILED/EXPIRED/REFUNDED
    qr_code           TEXT NOT NULL DEFAULT '',
    channel           TEXT NOT NULL DEFAULT 'precreate',
    subject           TEXT NOT NULL DEFAULT '',
    buyer_id          TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    paid_at           TEXT NOT NULL DEFAULT '',
    expires_at        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON payment_orders(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status ON payment_orders(status);

CREATE TABLE IF NOT EXISTS payment_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    provider     TEXT NOT NULL,
    event_id     TEXT NOT NULL UNIQUE,     -- 幂等键：provider + notify_id / trade_no + status
    order_no     TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL DEFAULT '',
    raw          TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generation_tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_uid         TEXT NOT NULL UNIQUE,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider_code    TEXT NOT NULL DEFAULT 'muse',
    model            TEXT NOT NULL DEFAULT 'muse-video',
    status           TEXT NOT NULL DEFAULT 'CREATED',
    prompt           TEXT NOT NULL DEFAULT '',
    final_prompt     TEXT NOT NULL DEFAULT '',
    params_json      TEXT NOT NULL DEFAULT '{}',
    reserved_points  INTEGER NOT NULL DEFAULT 0,
    settled_points   INTEGER NOT NULL DEFAULT 0,
    progress         INTEGER NOT NULL DEFAULT 0,
    provider_task_id TEXT NOT NULL DEFAULT '',
    attempt_no       INTEGER NOT NULL DEFAULT 0,
    thread_key       TEXT NOT NULL DEFAULT '',
    result_asset_id  INTEGER,
    result_text      TEXT NOT NULL DEFAULT '',
    error_code       TEXT NOT NULL DEFAULT '',
    error_message    TEXT NOT NULL DEFAULT '',
    queued_ahead     INTEGER NOT NULL DEFAULT 0,
    idempotency_key  TEXT NOT NULL DEFAULT '',
    client_ip        TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    started_at       TEXT NOT NULL DEFAULT '',
    finished_at      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON generation_tasks(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON generation_tasks(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idem
    ON generation_tasks(user_id, idempotency_key) WHERE idempotency_key <> '';

CREATE TABLE IF NOT EXISTS generation_attempts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
    attempt_no          INTEGER NOT NULL,
    provider_request_id TEXT NOT NULL DEFAULT '',
    provider_task_id    TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'SUBMITTED',
    error_code          TEXT NOT NULL DEFAULT '',
    error_message       TEXT NOT NULL DEFAULT '',
    started_at          TEXT NOT NULL,
    finished_at         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_attempts_task ON generation_attempts(task_id);

CREATE TABLE IF NOT EXISTS media_assets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    storage_key TEXT NOT NULL,               -- 相对 media_dir 的文件名
    kind        TEXT NOT NULL DEFAULT 'video',  -- video / image / upload
    mime        TEXT NOT NULL DEFAULT '',
    bytes       INTEGER NOT NULL DEFAULT 0,
    sha256      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'ready',
    source      TEXT NOT NULL DEFAULT 'generated',  -- generated / upload
    task_id     INTEGER,
    title       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_assets_user ON media_assets(user_id, id DESC);

CREATE TABLE IF NOT EXISTS model_providers (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT NOT NULL UNIQUE,
    name               TEXT NOT NULL DEFAULT '',
    enabled            INTEGER NOT NULL DEFAULT 1,
    base_url           TEXT NOT NULL DEFAULT '',
    api_key            TEXT NOT NULL DEFAULT '',
    capabilities_json  TEXT NOT NULL DEFAULT '{}',
    day_budget_points  INTEGER NOT NULL DEFAULT 0,
    concurrency        INTEGER NOT NULL DEFAULT 2,
    priority           INTEGER NOT NULL DEFAULT 100,
    note               TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pricing_rules (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_code  TEXT NOT NULL DEFAULT 'muse',
    model          TEXT NOT NULL DEFAULT 'muse-video',
    duration       INTEGER NOT NULL DEFAULT 10,
    resolution     TEXT NOT NULL DEFAULT '1080p',
    points         INTEGER NOT NULL,
    cost_minor     INTEGER NOT NULL DEFAULT 0,
    effective_at   TEXT NOT NULL,
    active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS prompt_templates (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    body     TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'general',
    sort     INTEGER NOT NULL DEFAULT 100,
    enabled  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id    INTEGER,
    action      TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT '',
    target_id   TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '',
    after_json  TEXT NOT NULL DEFAULT '',
    ip          TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(id DESC);

CREATE TABLE IF NOT EXISTS app_settings (
    k          TEXT PRIMARY KEY,
    v          TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_plus(minutes: int) -> str:
    return (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def connect() -> sqlite3.Connection:
    """新开一个连接（不走池，调用方自己负责 close）。"""
    CFG.ensure_dirs()
    conn = sqlite3.connect(CFG.db_path, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def acquire() -> sqlite3.Connection:
    """从池里取一个连接；池空则新建。"""
    with _LOCK:
        while _POOL:
            conn = _POOL.pop()
            try:
                conn.execute("SELECT 1")
                return conn
            except sqlite3.Error:      # 连接坏了就丢掉换一个
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
    return connect()


def release(conn: sqlite3.Connection) -> None:
    """归还连接：先回滚未提交事务，避免污染下一个请求。"""
    try:
        if conn.in_transaction:
            conn.rollback()
    except sqlite3.Error:
        pass
    with _LOCK:
        if len(_POOL) < _POOL_MAX:
            _POOL.append(conn)
            return
    try:
        conn.close()
    except sqlite3.Error:
        pass


def get_db():
    conn = acquire()
    try:
        yield conn
    finally:
        release(conn)


# ------------------------------------------------------------------ 查询助手
def q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def q1(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    return conn.execute(sql, params).fetchone()


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ 初始化
def _upgrade_resolutions(conn: sqlite3.Connection) -> None:
    """清晰度档位升级（幂等，每次启动都会跑，但只在需要时改数据）。

    - 老库只有 720p：把该档位原地改名成 1080p 基线价，不涨价；
    - 同时补出 2K 档，价格按 1080p 的倍率算；
    - 已经存在的 1080p / 2K 一律不动，管理员在后台改过的价不会被覆盖。
    """
    base = RESOLUTION_VALUES[0]
    rows = conn.execute(
        "SELECT id, duration, resolution, points FROM pricing_rules "
        "WHERE provider_code='muse' AND model='muse-video' AND active=1").fetchall()

    has_base = {int(r["duration"]) for r in rows if r["resolution"] == base}
    for r in rows:
        if str(r["resolution"]).strip().lower() not in ("720p", "720"):
            continue
        dur = int(r["duration"])
        if dur in has_base:      # 同时长已经有 1080p，这条只是历史残留
            conn.execute("DELETE FROM pricing_rules WHERE id=?", (r["id"],))
        else:
            conn.execute("UPDATE pricing_rules SET resolution=? WHERE id=?", (base, r["id"]))
            has_base.add(dur)

    rows = conn.execute(
        "SELECT duration, resolution, points FROM pricing_rules "
        "WHERE provider_code='muse' AND model='muse-video' AND active=1").fetchall()
    exists = {(int(r["duration"]), r["resolution"]) for r in rows}
    for r in rows:
        if r["resolution"] != base:
            continue
        dur, pts = int(r["duration"]), int(r["points"])
        for spec in RESOLUTIONS[1:]:
            key = (dur, spec["value"])
            if key in exists:
                continue
            conn.execute(
                "INSERT INTO pricing_rules(provider_code, model, duration, resolution, points, "
                "effective_at, active) VALUES('muse','muse-video',?,?,?,?,1)",
                (dur, spec["value"], max(1, pts * int(spec["multiplier"])), now()))
            exists.add(key)


def _sync_caps(conn: sqlite3.Connection) -> None:
    """能力里的清晰度档位跟随配置，避免老库还停在 720p/1080p。"""
    prow = q1(conn, "SELECT id, capabilities_json FROM model_providers WHERE code='muse'")
    if not prow:
        return
    try:
        caps = json.loads(prow["capabilities_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        caps = {}
    if caps.get("resolutions") != RESOLUTION_VALUES:
        caps["resolutions"] = list(RESOLUTION_VALUES)
        conn.execute("UPDATE model_providers SET capabilities_json=? WHERE id=?",
                     (json.dumps(caps, ensure_ascii=False), prow["id"]))


def _seed(conn: sqlite3.Connection) -> None:
    from .config import CFG as _cfg
    from .security import hash_password

    if not q1(conn, "SELECT id FROM users WHERE role = 'admin' LIMIT 1"):
        from . import guard

        if _cfg.admin_password:
            pwd = _cfg.admin_password
        elif guard.is_prod():
            # 生产环境不给默认口令：随机生成一次，只打在启动日志里，
            # 登录后请立刻改掉。（正常情况下 guard.check_boot() 已经拦在前面了，
            # 这里只是防止有人绕过守卫直接初始化数据库。）
            pwd = secrets.token_urlsafe(12)
            logging.getLogger("studio").warning(
                "未配置 ADMIN_PASSWORD，已生成随机管理员密码：%s（请立即登录后台修改）", pwd)
        else:
            pwd = "admin888"
            logging.getLogger("studio").warning(
                "本地环境，管理员账号 %s 使用内置默认口令，请勿用于生产", _cfg.admin_user)
        conn.execute(
            "INSERT INTO users(email, username, password_hash, role, status, created_at) "
            "VALUES(?,?,?,'admin','active',?)",
            (_cfg.admin_user if "@" in _cfg.admin_user else "admin@local",
             _cfg.admin_user, hash_password(pwd), now()))
        conn.execute("INSERT OR IGNORE INTO wallets(user_id, updated_at) "
                     "SELECT id, ? FROM users WHERE role='admin'", (now(),))

    if not q1(conn, "SELECT id FROM model_providers LIMIT 1"):
        caps = {
            "durations": [5, 10, 15, 20, 30],
            "aspect_ratios": ["9:16", "16:9", "1:1"],
            "resolutions": RESOLUTION_VALUES,
            "first_frame": True,
            "last_frame": False,
            "audio": False,
            "callback": False,
            "cancel": False,
            "max_prompt_chars": 2000,
        }
        conn.execute(
            "INSERT INTO model_providers(code, name, enabled, base_url, capabilities_json, note) "
            "VALUES('muse','Muse Video (号池网关)',1,?,?,?)",
            (_cfg.muse_base, json.dumps(caps, ensure_ascii=False),
             "复用现有 muse2api 上号链路，形如 OpenAI 的 HTTP 契约"))

    if not q1(conn, "SELECT id FROM pricing_rules WHERE model = 'muse-video' LIMIT 1"):
        # 定价：1080p 2 积分/秒 → 5 秒 10、10 秒 20、15 秒 30、20 秒 40、30 秒 60
        # 2K 按倍率翻倍 → 5 秒 20、10 秒 40 …
        for dur in (5, 10, 15, 20, 30):
            for res in RESOLUTIONS:
                conn.execute(
                    "INSERT INTO pricing_rules(provider_code, model, duration, resolution, points, "
                    "effective_at, active) VALUES('muse','muse-video',?,?,?,?,1)",
                    (dur, res["value"], dur * 2 * res["multiplier"], now()))
        # 图片：5 积分/张
        conn.execute(
            "INSERT INTO pricing_rules(provider_code, model, duration, resolution, points, "
            "effective_at, active) VALUES('muse','muse-image',0,'auto',5,?,1)", (now(),))

    _upgrade_resolutions(conn)
    _sync_caps(conn)

    if not q1(conn, "SELECT id FROM prompt_templates LIMIT 1"):
        _tpls = [
            ("产品广告", "镜头缓慢环绕产品，柔和顶光，高级质感，背景虚化，8 秒内完成一次推拉，"
                        "画面稳定无抖动", "commerce", 10),
            ("人物口播", "固定中景，人物正视镜头自然讲话，轻微呼吸感，柔和补光，背景干净", "talking", 20),
            ("风景空镜", "无人机缓慢前推，晨雾流动，光线由冷转暖，电影级调色，画面稳定", "scene", 30),
            ("图文动效", "以参考图为第一帧，元素依次浮现并轻微缩放，末尾定格，节奏轻快", "motion", 40),
            ("反差转场", "前 5 秒平静推进，第 5 秒硬切到高对比快剪，配节奏点，冲击力强", "hook", 50),
        ]
        for name, body, cat, sort in _tpls:
            conn.execute("INSERT INTO prompt_templates(name, body, category, sort) "
                         "VALUES(?,?,?,?)", (name, body, cat, sort))


def init_db() -> None:
    with _LOCK:
        conn = connect()
        try:
            # 日志模式只在启动时设一次；WAL 会持久写在库文件头里，
            # 之后的连接不用再设（并发设置会导致连接变只读，见 connect() 注释）
            try:
                mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if str(mode).lower() != "wal":
                    logger.warning("journal_mode 未能切到 WAL（当前 %s），"
                                   "并发写入会比较慢，但功能正常", mode)
            except sqlite3.OperationalError as exc:
                logger.warning("设置 WAL 失败：%s（不影响使用）", exc)

            conn.executescript(SCHEMA)
            # 轻量迁移：老库补列
            def _cols(table: str) -> set[str]:
                return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

            def _add(table: str, name: str, ddl: str) -> None:
                if name not in _cols(table):
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

            _add("users", "note", "TEXT NOT NULL DEFAULT ''")
            _add("generation_tasks", "result_text", "TEXT NOT NULL DEFAULT ''")
            _add("generation_tasks", "queued_ahead", "INTEGER NOT NULL DEFAULT 0")
            _add("payment_orders", "buyer_id", "TEXT NOT NULL DEFAULT ''")
            _seed(conn)
            conn.commit()
            # 这条连接常驻不关：只要它还在，WAL 的 -shm 文件就不会被删除，
            # 并发打开的新连接也不会退化成只读
            global _KEEPALIVE
            if _KEEPALIVE is None:
                _KEEPALIVE = conn
                conn = None
        finally:
            if conn is not None:
                conn.close()


# ------------------------------------------------------------------ 审计
def audit(conn: sqlite3.Connection, actor_id, action: str, target_type: str = "",
          target_id: str = "", before=None, after=None, ip: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_logs(actor_id, action, target_type, target_id, before_json, "
        "after_json, ip, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (actor_id, action, target_type, str(target_id),
         json.dumps(before, ensure_ascii=False) if before is not None else "",
         json.dumps(after, ensure_ascii=False) if after is not None else "",
         ip, now()))


# ------------------------------------------------------------------ 运行时设置
def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = q1(conn, "SELECT v FROM app_settings WHERE k = ?", (key,))
    return row["v"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO app_settings(k, v, updated_at) VALUES(?,?,?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
                 (key, value, now()))
