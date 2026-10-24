"""支付通道适配层（gateway）。

为什么要有这一层：
    业务代码（下单 / 查单 / 回调 / 退款）只认统一接口，不认具体支付公司。
    以后要接微信 Native 支付、易支付、手工转账，只需要：

        1. 新建 backend/app/payments/<你的通道>.py，实现下面 Adapter 里那 5 个方法；
        2. 在文件末尾的 REGISTRY 里加一行；
        3. .env 里设 PAYMENT_CHANNEL=<你的 code>，重启。

    订单表、积分入账、幂等、验签后的处理逻辑一行都不用改。

统一接口（Adapter）
------------------
    code / name / desc      通道标识与展示文案
    ready() -> bool         商户参数是否齐（决定能不能真收款）
    is_mock() -> bool       是否是模拟通道（只用于验收，不真收钱）
    create(order_no, amount_minor, subject, minutes) -> str
                            下单，返回二维码里要放的字符串
    query(order_no) -> dict 主动查单，返回统一结构：
                            {"trade_status": "TRADE_SUCCESS"|"", "trade_no": "...",
                             "total_amount": "200.00", "buyer_id": "..."}
    verify_notify(form) -> bool
                            异步通知验签
    selfcheck() -> dict     自检信息（打印到后台，方便排查密钥问题）
"""
from __future__ import annotations

import logging

from ..config import CFG
from . import alipay

log = logging.getLogger("studio.pay")


class GatewayError(Exception):
    """统一的下单/查单异常，message 可以直接展示给用户。"""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code


class BaseAdapter:
    code = "base"
    name = "未命名通道"
    desc = ""

    def ready(self) -> bool:
        return False

    def is_mock(self) -> bool:
        return False

    def create(self, order_no: str, amount_minor: int, subject: str, minutes: int) -> str:
        raise GatewayError("该支付通道未实现下单接口")

    def query(self, order_no: str) -> dict:
        raise GatewayError("该支付通道未实现查单接口")

    def verify_notify(self, form: dict) -> bool:
        return False

    def selfcheck(self) -> dict:
        return {"ok": self.ready()}


# ------------------------------------------------------------------ 支付宝当面付
class AlipayAdapter(BaseAdapter):
    code = "alipay"
    name = "支付宝当面付"
    desc = "当面付扫码，实时到账（推荐）"

    def ready(self) -> bool:
        return CFG.alipay_ready

    def create(self, order_no, amount_minor, subject, minutes):
        try:
            return alipay.precreate(order_no, amount_minor, subject, "%dm" % int(minutes))
        except alipay.AlipayError as exc:
            raise GatewayError("创建支付宝二维码失败：%s" % exc.message, exc.code) from exc

    def query(self, order_no):
        try:
            return alipay.query(order_no)
        except alipay.AlipayError as exc:
            raise GatewayError(exc.message, exc.code) from exc

    def verify_notify(self, form):
        return alipay.verify_notify(form)

    def selfcheck(self):
        info = alipay.selfcheck()
        info["notify_url"] = CFG.alipay_notify_url
        info["gateway"] = CFG.alipay_gateway
        return info


# ------------------------------------------------------------------ 模拟通道
class MockAdapter(BaseAdapter):
    """只用于没有商户参数时把「下单 → 扫码 → 到账 → 扣费」全链路跑通。

    二维码内容是假链接，靠后台的「模拟支付」按钮触发到账。
    生产环境必须把 ALIPAY_MOCK / PAYMENT_CHANNEL=mock 关掉。
    """

    code = "mock"
    name = "模拟支付"
    desc = "测试用，不真扣钱，点一下直接到账"

    def ready(self) -> bool:
        return True

    def is_mock(self) -> bool:
        return True

    def create(self, order_no, amount_minor, subject, minutes):
        return "https://qr.alipay.com/mock/" + order_no

    def query(self, order_no):
        # 模拟通道的到账由 POST /api/payments/mock-pay/{order_no} 触发，
        # 查单永远返回「未支付」，避免凭空到账。
        return {}

    def verify_notify(self, form):
        return False

    def selfcheck(self):
        return {"ok": True, "mock": True}


# ------------------------------------------------------------------ 注册表
# 想加新通道：实现一个 BaseAdapter 子类，然后在这里加一行 {"code": 实例}
REGISTRY: dict[str, BaseAdapter] = {
    a.code: a for a in (AlipayAdapter(), MockAdapter())
}

ORDER_PENDING = ("CREATED", "PENDING")


def get(code: str) -> BaseAdapter | None:
    return REGISTRY.get((code or "").strip().lower())


def resolve_code() -> str:
    """当前生效的通道 code；一个都不可用时返回空串。

    优先级：PAYMENT_CHANNEL 指定 > 支付宝参数齐全 > 模拟模式 > 无
    显式指定了但商户参数不齐，直接判为「不可用」（不偷偷回落到模拟通道）。
    """
    want = CFG.payment_channel
    if want in ("none", "off", "0", "disabled", "close"):
        return ""          # 显式关掉充值
    if want:
        adapter = REGISTRY.get(want)
        if adapter is None:
            log.warning("PAYMENT_CHANNEL=%s 没有对应适配器，改为自动选择", want)
        elif adapter.is_mock() or adapter.ready():
            return want
        else:
            log.warning("PAYMENT_CHANNEL=%s 的商户参数不齐，充值通道不可用", want)
            return ""
    if CFG.alipay_mock:
        return "mock"
    if alipay.ready():
        return "alipay"
    return ""


def current() -> BaseAdapter | None:
    return get(resolve_code())


def available() -> list[dict]:
    """给前端/后台看的通道清单（不含密钥）。"""
    active = resolve_code()
    out = []
    for code, a in REGISTRY.items():
        out.append({
            "code": code,
            "name": a.name,
            "desc": a.desc,
            "ready": bool(a.ready()),
            "mock": a.is_mock(),
            "active": code == active,
            "enabled": bool(a.ready()) and not a.is_mock(),
        })
    return out
