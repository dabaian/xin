"""全局配置：全部来自环境变量 / .env，代码里不硬编码任何密钥。

⚠️ 设计原则（对齐 V3 架构文档 §2）：
   前端永远不持有模型密钥、支付私钥；所有高权限调用都在后端完成。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(BASE_DIR)


def _load_dotenv() -> None:
    """加载 ROOT_DIR/.env，不覆盖已存在的系统环境变量。"""
    for name in (".env", ".env.local"):
        path = os.path.join(ROOT_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                        v = v[1:-1]
                    v = v.replace("\\n", "\n")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except OSError:
            pass


_load_dotenv()


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)) or default)
    except ValueError:
        return default


def _bool(key: str, default: bool = False) -> bool:
    return _env(key, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def _read_key(inline_value: str, path_value: str) -> str:
    """密钥既可以直接写在 env 里，也可以指向文件（推荐，便于 chmod 600）。"""
    if inline_value:
        # 支持把 PEM 压成一行、用 \n 转义的形式
        return inline_value.replace("\\n", "\n").strip()
    if path_value and os.path.isfile(path_value):
        try:
            with open(path_value, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""
    return ""


# ------------------------------------------------------------------ 充值套餐
# 定价口径：1 元 = 10 积分。price 单位是「分」，整数，避免浮点误差。
DEFAULT_PACKAGES = [
    {"id": "starter", "name": "体验包", "points": 100, "price": 1000, "bonus": 0, "tag": ""},
    {"id": "basic", "name": "基础包", "points": 500, "price": 5000, "bonus": 50, "tag": "最受欢迎"},
    {"id": "pro", "name": "专业包", "points": 1200, "price": 12000, "bonus": 200, "tag": ""},
    {"id": "studio", "name": "工作室包", "points": 3000, "price": 30000, "bonus": 800, "tag": "性价比最高"},
]

# 计费：图片 5 积分/张；视频按 2 积分/秒（5 秒 = 10，10 秒 = 20）
POINTS_PER_IMAGE = 5
POINTS_PER_SECOND = 2


@dataclass
class Settings:
    # ---------- 基础 ----------
    app_name: str = field(default_factory=lambda: _env("APP_NAME", "Muse Studio"))
    app_env: str = field(default_factory=lambda: _env("APP_ENV", "prod"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", ""))
    host: str = field(default_factory=lambda: _env("BIND_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("BIND_PORT", 8080))
    public_base: str = field(default_factory=lambda: _env("PUBLIC_BASE", "").rstrip("/"))

    # ---------- 目录 ----------
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", os.path.join(ROOT_DIR, "data")))
    frontend_dir: str = field(default_factory=lambda: _env("FRONTEND_DIR", os.path.join(ROOT_DIR, "frontend")))
    release_dir: str = field(default_factory=lambda: _env("RELEASE_DIR", os.path.join(ROOT_DIR, "releases")))

    # ---------- 管理员 ----------
    admin_user: str = field(default_factory=lambda: _env("ADMIN_USERNAME", "admin"))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD", ""))

    # ---------- 上游：muse2api 号池网关 ----------
    # 与现有 muse2api 完全兼容：/v1/videos/generations、/v1/chat/completions、
    # /v1/models、/v1/media/{name}
    muse_base: str = field(default_factory=lambda: _env("MUSE_BASE", "http://127.0.0.1:18610").rstrip("/"))
    muse_key: str = field(default_factory=lambda: _env("MUSE_KEY", ""))
    muse_model: str = field(default_factory=lambda: _env("MUSE_MODEL", "muse-video"))
    muse_timeout: int = field(default_factory=lambda: _int("MUSE_TIMEOUT", 600))
    muse_poll_interval: float = field(default_factory=lambda: float(_env("MUSE_POLL_INTERVAL", "4") or 4))
    muse_verify_ssl: bool = field(default_factory=lambda: _bool("MUSE_VERIFY_SSL", True))
    # 号池一般是 127.0.0.1 / 内网地址，默认直连、不吃系统 HTTP_PROXY
    #（本机挂 Clash 之类会把内网请求也接管，报 502 / os error 10061）
    muse_use_proxy: bool = field(default_factory=lambda: _bool("MUSE_USE_PROXY", False))

    # ---------- 计费 ----------
    # 视频 = 2 积分/秒（5 秒 10、10 秒 20）；图片 = 5 积分/张
    points_per_second: int = field(default_factory=lambda: _int("POINTS_PER_SECOND",
                                                               POINTS_PER_SECOND))
    points_per_image: int = field(default_factory=lambda: _int("POINTS_PER_IMAGE",
                                                              POINTS_PER_IMAGE))
    # 新用户赠送积分（= 2 张图 + 1 条 5 秒视频，足够试手）
    signup_bonus: int = field(default_factory=lambda: _int("SIGNUP_BONUS", 20))
    # 单用户最多同时挂几个任务（防刷）
    max_active_tasks: int = field(default_factory=lambda: _int("MAX_ACTIVE_TASKS", 2))

    # ---------- 支付宝当面付（扫码充值） ----------
    alipay_app_id: str = field(default_factory=lambda: _env("ALIPAY_APP_ID", ""))
    alipay_private_key: str = field(
        default_factory=lambda: _read_key(_env("ALIPAY_PRIVATE_KEY"), _env("ALIPAY_PRIVATE_KEY_PATH")))
    alipay_public_key: str = field(
        default_factory=lambda: _read_key(_env("ALIPAY_PUBLIC_KEY"), _env("ALIPAY_PUBLIC_KEY_PATH")))
    alipay_gateway: str = field(default_factory=lambda: _env(
        "ALIPAY_GATEWAY", "https://openapi.alipay.com/gateway.do"))
    alipay_notify_url: str = field(default_factory=lambda: _env("ALIPAY_NOTIFY_URL", ""))
    alipay_return_url: str = field(default_factory=lambda: _env("ALIPAY_RETURN_URL", ""))
    # 支付订单超时（分钟），支付宝当面付 qr_code_timeout_express
    alipay_timeout: str = field(default_factory=lambda: _env("ALIPAY_TIMEOUT", "30m"))
    # 模拟支付：只用于没有商户密钥时跑通/验收流程，生产必须关掉
    alipay_mock: bool = field(default_factory=lambda: _bool("ALIPAY_MOCK", False))

    # ---------- 桌面端 ----------
    desktop_version: str = field(default_factory=lambda: _env("DESKTOP_VERSION", "2.0.0"))
    desktop_setup_name: str = field(default_factory=lambda: _env("DESKTOP_SETUP_NAME", "MuseStudio-Setup.exe"))

    # ---------- 行为开关 ----------
    allow_register: bool = field(default_factory=lambda: _bool("ALLOW_REGISTER", True))
    rate_limit_per_min: int = field(default_factory=lambda: _int("RATE_LIMIT_PER_MIN", 30))

    # ------------------------------------------------------------------
    def ensure_dirs(self) -> None:
        for d in (self.data_dir, os.path.join(self.data_dir, "media"),
                  os.path.join(self.data_dir, "uploads"), self.release_dir):
            os.makedirs(d, exist_ok=True)

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "studio.db")

    @property
    def media_dir(self) -> str:
        return os.path.join(self.data_dir, "media")

    @property
    def upload_dir(self) -> str:
        return os.path.join(self.data_dir, "uploads")

    @property
    def packages(self) -> list[dict]:
        raw = _env("RECHARGE_PACKAGES", "")
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, list) and data:
                    return data
            except json.JSONDecodeError:
                pass
        return DEFAULT_PACKAGES

    @property
    def alipay_ready(self) -> bool:
        return bool(self.alipay_app_id and self.alipay_private_key and self.alipay_public_key)

    @property
    def site_base(self) -> str:
        """对外访问地址；未配置时由请求头兜底。"""
        return self.public_base


CFG = Settings()
CFG.ensure_dirs()

if not CFG.secret_key:
    # 首次启动自动生成并落盘，保证重启后会话不失效
    _keyfile = os.path.join(CFG.data_dir, ".secret")
    if os.path.isfile(_keyfile):
        with open(_keyfile, encoding="utf-8") as _f:
            CFG.secret_key = _f.read().strip()
    if not CFG.secret_key:
        import secrets as _s

        CFG.secret_key = _s.token_urlsafe(48)
        with open(_keyfile, "w", encoding="utf-8") as _f:
            _f.write(CFG.secret_key)
        try:
            os.chmod(_keyfile, 0o600)
        except OSError:
            pass
