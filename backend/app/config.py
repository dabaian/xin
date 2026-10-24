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
#
# 赠送规则（单一来源，别在套餐里手写 bonus，容易和规则打架）：
#   充值 200 ~ 499 元  → 赠 10%
#   充值 500 ~ 999 元  → 赠 15%
#   充值满 1000 元     → 赠 20%
#   不足 200 元        → 不赠
# 自定义金额同样走这套规则。
BONUS_TIERS = [
    {"min_minor": 100000, "num": 1, "den": 5, "label": "满 1000 元赠 20%"},
    {"min_minor": 50000, "num": 3, "den": 20, "label": "满 500 元赠 15%"},
    {"min_minor": 20000, "num": 1, "den": 10, "label": "满 200 元赠 10%"},
]

# 自定义充值：单位元，最低 10 元，最高 2 万（防手滑填错）
CUSTOM_MIN_YUAN = 10
CUSTOM_MAX_YUAN = 20000

DEFAULT_PACKAGES = [
    {"id": "starter", "name": "体验包", "points": 100, "price": 1000, "tag": ""},
    {"id": "basic", "name": "基础包", "points": 500, "price": 5000, "tag": ""},
    {"id": "standard", "name": "标准包", "points": 2000, "price": 20000, "tag": "满 200 赠 10%"},
    {"id": "pro", "name": "专业包", "points": 5000, "price": 50000, "tag": "满 500 赠 15%"},
    {"id": "studio", "name": "工作室包", "points": 10000, "price": 100000, "tag": "满 1000 赠 20%"},
    {"id": "enterprise", "name": "企业包", "points": 20000, "price": 200000, "tag": "最划算"},
]


def bonus_tier(price_minor: int) -> dict | None:
    """命中的赠送档位；不满足门槛返回 None。"""
    m = int(price_minor or 0)
    for tier in BONUS_TIERS:          # 已按门槛从高到低排列
        if m >= tier["min_minor"]:
            return tier
    return None


def bonus_for(points: int, price_minor: int) -> int:
    """按充值金额算赠送积分（整数运算，不出现浮点误差）。"""
    tier = bonus_tier(price_minor)
    if not tier:
        return 0
    return int(points or 0) * int(tier["num"]) // int(tier["den"])


def custom_package(yuan) -> dict:
    """自定义金额（元）→ 套餐结构。不合法时抛 ValueError，消息可直接给用户看。"""
    try:
        y = round(float(str(yuan).strip()), 2)
    except (TypeError, ValueError):
        raise ValueError("充值金额格式不正确") from None
    if y != y or y in (float("inf"), float("-inf")):
        raise ValueError("充值金额格式不正确")
    if y < CUSTOM_MIN_YUAN:
        raise ValueError("自定义充值不能低于 %d 元" % CUSTOM_MIN_YUAN)
    if y > CUSTOM_MAX_YUAN:
        raise ValueError("单笔充值不能超过 %d 元，大额请分笔或联系客服" % CUSTOM_MAX_YUAN)

    price_minor = int(round(y * 100))
    points = max(1, price_minor // 10)       # 1 元 = 10 积分
    bonus = bonus_for(points, price_minor)
    tier = bonus_tier(price_minor)
    return {
        "id": "custom",
        "name": "自定义充值 %s 元" % ("%g" % y),
        "points": points,
        "bonus": bonus,
        "total_points": points + bonus,
        "price": price_minor,
        "price_minor": price_minor,
        "price_yuan": "%.2f" % y,
        "bonus_label": ("赠 %d%%" % round(100 * tier["num"] / tier["den"])) if tier else "",
        "tag": "",
    }


def package_with_bonus(pkg: dict) -> dict:
    """把套餐补上 bonus / total_points / bonus_label。"""
    p = dict(pkg)
    points = int(p.get("points") or 0)
    price = int(p.get("price") or 0)
    # 显式写了 bonus 的以显式为准（方便临时做活动），否则按规则算
    bonus = int(p.get("bonus")) if p.get("bonus") not in (None, "") else bonus_for(points, price)
    p["points"] = points
    p["price"] = price
    p["bonus"] = max(0, bonus)
    p["total_points"] = points + p["bonus"]
    tier = bonus_tier(price)
    p["bonus_label"] = ("赠 %d%%" % round(100 * tier["num"] / tier["den"])) if tier else ""
    p["price_yuan"] = round(price / 100.0, 2)
    return p

# 计费：图片 5 积分/张；视频按 2 积分/秒（5 秒 = 10，10 秒 = 20）
# 上面的「每秒积分」是 1080p 基准价，2K 在基准价上再乘倍率。
POINTS_PER_IMAGE = 5
POINTS_PER_SECOND = 2

# ------------------------------------------------------------------ 清晰度
# 展示顺序即前端下拉顺序，第一项为默认值。
# value 会原样透传给号池的 resolution 字段，改这里要确认上游认这个值。
RESOLUTIONS = [
    {"value": "1080p", "label": "1080p 高清", "multiplier": 1, "tag": "推荐"},
    {"value": "2k", "label": "2K 超清", "multiplier": 2, "tag": ""},
]
DEFAULT_RESOLUTION = RESOLUTIONS[0]["value"]
RESOLUTION_VALUES = [r["value"] for r in RESOLUTIONS]
RESOLUTION_MULTIPLIER = {r["value"]: r["multiplier"] for r in RESOLUTIONS}


def resolution_multiplier(value: str) -> int:
    """清晰度倍率；未知档位按基准价（1 倍）算，不会白送也不会多扣。"""
    return int(RESOLUTION_MULTIPLIER.get(normalize_resolution(value), 1))


def normalize_resolution(value: str | None) -> str:
    """把前端/历史数据的各种写法归一到配置里的档位。"""
    v = (value or "").strip().lower().replace(" ", "")
    if v in ("2k", "1440p", "2.5k", "qhd"):
        return "2k"
    if v in ("1080", "1080p", "fhd", "fullhd", "1920x1080"):
        return "1080p"
    if v in ("720", "720p", "hd"):
        # 老版本只用过 720p，等价看作是当时的默认档
        return "1080p"
    return DEFAULT_RESOLUTION


@dataclass
class Settings:
    # ---------- 基础 ----------
    app_name: str = field(default_factory=lambda: _env("APP_NAME", "Viora AI"))
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

    # ---------- 注册验证码 ----------
    captcha_enabled: bool = field(default_factory=lambda: _bool("CAPTCHA_ENABLED", True))
    captcha_ttl: int = field(default_factory=lambda: _int("CAPTCHA_TTL", 300))

    # ---------- 邮件（注册 / 验证码登录） ----------
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: _int("SMTP_PORT", 465))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER", ""))
    smtp_pass: str = field(default_factory=lambda: _env("SMTP_PASS", ""))
    smtp_from: str = field(default_factory=lambda: _env("SMTP_FROM", ""))
    smtp_ssl: bool = field(default_factory=lambda: _bool("SMTP_SSL", True))
    smtp_starttls: bool = field(default_factory=lambda: _bool("SMTP_STARTTLS", False))
    # 没配 SMTP 时是否允许把验证码回传给前端（本地调试用，线上务必关）
    mail_echo: bool = field(default_factory=lambda: _bool("MAIL_ECHO", False))
    email_code_ttl: int = field(default_factory=lambda: _int("EMAIL_CODE_TTL", 600))
    email_code_required: bool = field(default_factory=lambda: _bool("EMAIL_CODE_REQUIRED", True))

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
    # 支付通道：留空=自动（配了支付宝参数就真收款，否则模拟）。
    # 想接微信 / 易支付 / 手工转账，在 payments/gateway.py 的 REGISTRY 里加一个适配器，
    # 然后把这里设成对应 code 即可，业务层不用改。
    payment_channel: str = field(default_factory=lambda: _env("PAYMENT_CHANNEL", "").strip().lower())

    # ---------- 前后端分离 / 对外接入 ----------
    # 后端 API 对外前缀（默认空）。后端挂在域名子路径（如 https://x.com/viora/api）时填 /viora。
    api_base: str = field(default_factory=lambda: _env("API_BASE", "").rstrip("/"))
    # 允跨域调用前端/接口的来源，逗号分隔；* 表示全放开（仅调试，带 Cookie 时不能用 *）
    cors_origins: str = field(default_factory=lambda: _env("CORS_ORIGINS", ""))

    # ---------- 桌面端 ----------
    desktop_version: str = field(default_factory=lambda: _env("DESKTOP_VERSION", "2.0.0"))
    desktop_setup_name: str = field(default_factory=lambda: _env("DESKTOP_SETUP_NAME", "VioraAI-Setup.exe"))

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
                    return [package_with_bonus(p) for p in data]
            except json.JSONDecodeError:
                pass
        return [package_with_bonus(p) for p in DEFAULT_PACKAGES]

    @property
    def alipay_ready(self) -> bool:
        return bool(self.alipay_app_id and self.alipay_private_key and self.alipay_public_key)

    @property
    def cors_list(self) -> list[str]:
        raw = (self.cors_origins or "").replace(";", ",")
        return [x.strip().rstrip("/") for x in raw.split(",") if x.strip()]

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
