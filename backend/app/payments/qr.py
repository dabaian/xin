"""把支付宝返回的 qr_code 字符串渲染成二维码（服务端出图，不依赖任何 CDN）。"""
from __future__ import annotations

import io

import segno


def svg(text: str, border: int = 2) -> str:
    qr = segno.make(text, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=1, border=border, dark="#111418", light=None,
            xmldecl=False, svgns=True, nl=False)
    return buf.getvalue().decode("utf-8")


def png(text: str, scale: int = 8, border: int = 2) -> bytes:
    qr = segno.make(text, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=border, dark="#111418", light="white")
    return buf.getvalue()


def data_uri(text: str, scale: int = 8) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(png(text, scale)).decode()
