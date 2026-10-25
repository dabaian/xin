"""注册图形验证码。

设计要点：
 1. 答案不落库，只存 SHA-256 哈希——库被读走也还原不出验证码；
 2. 一次性：校验过的立即作废，答错也作废，逼着每次尝试都重新取图；
 3. 有 TTL（默认 5 分钟），过期即失效；
 4. 字体缺失时自动降级到 Pillow 内置字体，服务器没装中文字体也能出图。
"""
from __future__ import annotations

import base64
import io
import random
import uuid

from .config import CFG
from .security import token_hash

# 去掉 0/O、1/l/I 这类肉眼容易看错的字符，减少「明明对却过不了」
_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_LEN = 4
_W, _H = 132, 46

# 常见系统字体，命中哪个用哪个（服务器上通常有 DejaVu）
_FONTS = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _font(size: int):
    from PIL import ImageFont

    for path in _FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)   # Pillow ≥ 10
    except TypeError:                              # 更老的 Pillow
        return ImageFont.load_default()


def _render(code: str) -> bytes:
    """把字符画成带干扰的 PNG。"""
    from PIL import Image, ImageDraw, ImageFilter

    img = Image.new("RGB", (_W, _H), (247, 248, 250))
    d = ImageDraw.Draw(img)

    # 背景噪点
    for _ in range(160):
        d.point((random.randint(0, _W), random.randint(0, _H)),
                fill=(random.randint(150, 215), random.randint(150, 215), random.randint(150, 215)))

    # 干扰线
    for _ in range(5):
        d.line([(random.randint(0, _W), random.randint(0, _H)),
                (random.randint(0, _W), random.randint(0, _H))],
               fill=(random.randint(120, 200), random.randint(120, 200), random.randint(120, 200)),
               width=1)

    # 字符逐个旋转贴图
    slot = (_W - 20) // _LEN
    for i, ch in enumerate(code):
        size = random.randint(24, 27)
        f = _font(size)
        box = d.textbbox((0, 0), ch, font=f)
        w, h = box[2] - box[0], box[3] - box[1]
        layer = Image.new("RGBA", (w + 12, h + 12), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.text((6 - box[0], 6 - box[1]), ch, font=f,
                fill=(random.randint(25, 85), random.randint(25, 85), random.randint(40, 105)))
        layer = layer.rotate(random.uniform(-22, 22), expand=True, resample=Image.BICUBIC)
        x = 10 + i * slot + random.randint(-2, 2)
        # 贴右边时向内收，避免最后一个字符被裁掉
        x = min(x, _W - layer.width - 4)
        y = (_H - layer.height) // 2 + random.randint(-3, 3)
        img.paste(layer, (max(2, x), max(2, y)), layer)

    img = img.filter(ImageFilter.SMOOTH)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def enabled() -> bool:
    """是否启用任何形式的人机验证。"""
    return provider() != "off"


def provider() -> str:
    """当前生效的验证方式：geetest / image / off。

    CAPTCHA_PROVIDER 四种取值：
      auto      配了极验就用极验，否则用自绘图形码（默认）
      geetest   强制极验；没配 ID/KEY 时视为关闭，避免半配状态放行
      image     强制自绘图形码（内网/无法访问极验时用）
      off       全部关闭（不建议线上用）
    """
    from . import geetest

    want = (CFG.captcha_provider or "auto").strip().lower()
    if want in ("off", "none", "0", "false", "disabled"):
        return "off"
    if want == "geetest":
        return "geetest" if geetest.configured() else "off"
    if want == "image":
        return "image" if CFG.captcha_enabled else "off"
    # auto
    if geetest.configured():
        return "geetest"
    return "image" if CFG.captcha_enabled else "off"


def verify_any(conn, data, ip: str = "") -> tuple[bool, str]:
    """按当前 provider 校验一次请求，返回 (是否通过, 失败原因)。

    data 可以是任意带这些属性的对象（pydantic 模型 / dict 都行）：
      geetest: lot_number / captcha_output / pass_token / gen_time
      image:   captcha_id / captcha_code
    """
    from . import geetest

    def pick(name: str) -> str:
        if isinstance(data, dict):
            return str(data.get(name) or "")
        return str(getattr(data, name, "") or "")

    mode = provider()
    if mode == "off":
        return True, ""
    if mode == "geetest":
        return geetest.verify(pick("lot_number"), pick("captcha_output"),
                              pick("pass_token"), pick("gen_time"), ip=ip)
    return verify(conn, pick("captcha_id"), pick("captcha_code"))


def issue(conn) -> dict:
    """签发一张图形验证码，返回可直接塞进 <img src> 的 data URI。

    只有当前 provider 是 image 时才发图；走极验时返回 enabled=False，
    前端据此不渲染图形码那一栏。
    """
    if provider() != "image":
        return {"ok": True, "enabled": False, "provider": provider()}
    code = "".join(random.choice(_ALPHABET) for _ in range(_LEN))
    cid = uuid.uuid4().hex
    # 顺手清掉过期记录，避免表无限增长（只在少量概率下做，省 IO）
    if random.random() < 0.2:
        conn.execute("DELETE FROM captchas WHERE expires_at < ?", (_now(),))
    conn.execute(
        "INSERT INTO captchas(id, code_hash, created_at, expires_at) VALUES(?,?,?,?)",
        (cid, token_hash(code.upper()), _now(), _expires()))
    conn.commit()
    return {
        "ok": True,
        "enabled": True,
        "id": cid,
        "image": "data:image/png;base64," + base64.b64encode(_render(code)).decode(),
        "expires_in": int(CFG.captcha_ttl),
    }


def verify(conn, cid: str, code: str) -> tuple[bool, str]:
    """校验图形验证码并作废。返回 (是否通过, 失败原因)。"""
    if provider() != "image":
        return True, ""
    cid = (cid or "").strip()
    code = (code or "").strip().upper()
    if not cid or not code:
        return False, "请填写图形验证码"
    row = conn.execute("SELECT * FROM captchas WHERE id=?", (cid,)).fetchone()
    if not row:
        return False, "验证码已失效，请点击图片刷新"
    # 一次性：先删再比对，答错也得重新取图
    conn.execute("DELETE FROM captchas WHERE id=?", (cid,))
    conn.commit()
    if row["expires_at"] < _now():
        return False, "验证码已过期，请点击图片刷新"
    if row["code_hash"] != token_hash(code):
        return False, "验证码不正确，请重新输入"
    return True, ""


def purge(conn) -> int:
    cur = conn.execute("DELETE FROM captchas WHERE expires_at < ?", (_now(),))
    conn.commit()
    return cur.rowcount


def _now() -> str:
    from . import db

    return db.now()


def _expires() -> str:
    import math

    from . import db

    return db.now_plus(max(1, math.ceil(int(CFG.captcha_ttl) / 60)))
