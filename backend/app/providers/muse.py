"""Muse 适配器：直接对接现有的 muse2api 号池网关。

契约（与 muse2api 保持一致，无需改动上游）：
    POST /v1/videos/generations   {prompt, model, duration, aspect_ratio, image_url,
                                   thread_key, reuse_thread, extra, timeout}
    GET  /v1/videos/generations/{task_id}
    GET  /v1/media/{name}         成品文件（需 Bearer）
    GET  /v1/models
    GET  /healthz

关键点：上游 task_id 只在第一次提交时生成，之后所有查询都用它，
        所以「供应商超时」不等于「生成失败」，必须先查单（V3 §7）。
"""
from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

from ..config import CFG
from .base import GenRequest, ProviderError, StatusResult, SubmitResult, VideoProvider

DEFAULT_CAPS = {
    "durations": [5, 10, 15, 20, 30],
    "aspect_ratios": ["9:16", "16:9", "1:1"],
    "resolutions": ["1080p", "2k"],
    "first_frame": True,
    "last_frame": False,
    "audio": False,
    "callback": False,
    "cancel": False,
    "max_prompt_chars": 2000,
    "notes": "Muse Video 实际固定渲染约 10 秒；其余时长由提示词约束",
}

# 上游状态 → 本地状态
_STATUS_MAP = {
    "queued": "QUEUED",
    "pending": "QUEUED",
    "processing": "RUNNING",
    "running": "RUNNING",
    "in_progress": "RUNNING",
    "completed": "SUCCEEDED",
    "succeeded": "SUCCEEDED",
    "success": "SUCCEEDED",
    "failed": "FAILED",
    "error": "FAILED",
    "cancelled": "CANCELLED",
    "canceled": "CANCELLED",
}


class MuseProvider(VideoProvider):
    code = "muse"
    name = "Muse Video"

    def __init__(self, base_url: str = "", api_key: str = ""):
        self.base_url = (base_url or CFG.muse_base).rstrip("/")
        self.api_key = api_key or CFG.muse_key

    # ------------------------------------------------------------ HTTP
    def _opener(self):
        """默认直连上游，不吃系统 HTTP_PROXY。

        号池多是 127.0.0.1 / 内网地址，一旦本机挂着 Clash 之类的代理，
        请求会被代理接管并返回 502 upstream connect failed（os error 10061），
        看上去像「号池挂了」，实际是代理拦了内网地址。
        真要让号池走代理时，.env 里设 MUSE_USE_PROXY=1。
        """
        handlers = []
        if not CFG.muse_use_proxy:
            handlers.append(urllib.request.ProxyHandler({}))
        if not CFG.muse_verify_ssl:
            handlers.append(urllib.request.HTTPSHandler(
                context=ssl._create_unverified_context()))  # noqa: SLF001
        return urllib.request.build_opener(*handlers)

    @staticmethod
    def _looks_like_proxy_failure(text: str) -> bool:
        low = (text or "").lower()
        return any(k in low for k in ("upstream connect failed", "10061",
                                      "connection refused", "econnrefused"))

    def _call(self, method: str, path: str, payload: dict | None = None,
              timeout: float = 180.0, raw_stream: bool = False):
        url = self.base_url + path
        data = None
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            resp = self._opener().open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read()[:400].decode("utf-8", "ignore").strip()
            retryable = exc.code in (408, 425, 429, 500, 502, 503, 504)
            detail = "号池 %s 返回 %s：%s" % (self.base_url, exc.code,
                                             body or exc.reason)
            if exc.code in (502, 503, 504) and self._looks_like_proxy_failure(body):
                detail += ("｜该错误像是本机代理接管了内网请求："
                           "确认号池地址可直连，或设置 MUSE_USE_PROXY=1")
            raise ProviderError("HTTP_%s" % exc.code, detail,
                                retryable=retryable) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (ConnectionRefusedError,)) or \
                    self._looks_like_proxy_failure(str(reason)):
                detail = ("号池 %s 拒绝连接：该地址没有服务在监听。"
                          "请确认号池已启动，且 MUSE_BASE 指向正确端口。"
                          % self.base_url)
            elif isinstance(reason, (socket.timeout, TimeoutError)):
                detail = "号池 %s 响应超时（%ss），检查网络或号池负载" % (
                    self.base_url, int(timeout))
            elif "certificate" in str(reason).lower():
                detail = ("号池 %s 的 HTTPS 证书校验失败；"
                          "自签证书可设 MUSE_VERIFY_SSL=0" % self.base_url)
            else:
                detail = "连不上号池 %s：%s" % (self.base_url, reason)
            raise ProviderError("UPSTREAM_UNREACHABLE", detail, retryable=True) from exc
        except (TimeoutError, OSError) as exc:
            raise ProviderError("UPSTREAM_UNREACHABLE",
                                "连不上号池 %s：%s" % (self.base_url, exc),
                                retryable=True) from exc
        if raw_stream:
            return resp
        try:
            return json.loads(resp.read().decode("utf-8", "ignore") or "{}")
        finally:
            resp.close()

    # ------------------------------------------------------------ 能力 / 报价
    def capabilities(self) -> dict:
        caps = dict(DEFAULT_CAPS)
        try:
            info = self._call("GET", "/v1/models", timeout=8)
            ids = [m.get("id") for m in (info.get("data") or []) if isinstance(m, dict)]
            if ids:
                caps["upstream_models"] = ids
            caps["reachable"] = True
        except ProviderError as exc:
            caps["reachable"] = False
            caps["error"] = exc.message
        return caps

    def estimate_cost(self, req: GenRequest) -> dict:
        # 价格由 pricing_rules 决定，上层查库；这里给一个兜底
        if req.kind == "image":
            return {"points": CFG.points_per_image, "cost_minor": 0}
        base = max(1, int(req.duration or 10)) * CFG.points_per_second
        return {"points": base, "cost_minor": 0}

    # ------------------------------------------------------------ 提交 / 查单
    def submit(self, req: GenRequest, idempotency_key: str) -> SubmitResult:
        if req.kind == "image":
            payload = {"prompt": req.prompt, "model": "muse-image",
                       "size": "1024x1024", "aspect_ratio": req.aspect_ratio or "1:1",
                       "timeout": req.timeout or 240}
            if req.image_url:
                payload["reference_image"] = req.image_url
            data = self._call("POST", "/v1/images/tasks", payload, timeout=180.0)
        else:
            payload = {
                "prompt": req.prompt,
                "model": req.model or "muse-video",
                "duration": max(3, min(int(req.duration or 10), 30)),
                "aspect_ratio": req.aspect_ratio or "9:16",
                "resolution": req.resolution or "1080p",
                "timeout": req.timeout or CFG.muse_timeout,
            }
            if req.image_url:
                payload["image_url"] = req.image_url
            if req.reuse_thread:
                payload["reuse_thread"] = True
                payload["thread_key"] = req.thread_key or idempotency_key[:24]
            if req.extra:
                payload["extra"] = req.extra
            data = self._call("POST", "/v1/videos/generations", payload, timeout=180.0)
        tid = str(data.get("task_id") or data.get("id") or "")
        if not tid:
            raise ProviderError("BAD_RESPONSE", "上游没有返回 task_id：%s" % json.dumps(
                data, ensure_ascii=False)[:200], retryable=False)
        return SubmitResult(provider_task_id=tid, raw=data)

    def get_status(self, provider_task_id: str, kind: str = "video") -> StatusResult:
        tid = urllib.parse.quote(str(provider_task_id), safe="")
        prefix = "/v1/images/tasks/" if kind == "image" else "/v1/videos/generations/"
        data = self._call("GET", prefix + tid, timeout=40.0)
        up = str(data.get("status") or "").lower()
        local = _STATUS_MAP.get(up, "RUNNING")

        media_url = ""
        result = data.get("result")
        if isinstance(result, dict):
            media_url = result.get("url") or ""
            if not media_url:
                for item in (result.get("data") or []):
                    u = item.get("url") if isinstance(item, dict) else item
                    if u:
                        media_url = u
                        break
        elif isinstance(result, str) and result.startswith("/v1/media/"):
            media_url = result
        if not media_url and isinstance(data.get("url"), str):
            media_url = data["url"]

        error = str(data.get("error") or "")
        text = str(data.get("text") or (result.get("text") if isinstance(result, dict) else "") or "")
        if "仅返回文本" in error:
            text = error.split("仅返回文本:", 1)[-1].strip() or text
            error = ""

        # 上游说完成但没有成品 → 归入待人工对账，不自动扣费
        if local == "SUCCEEDED" and not media_url:
            local = "NEEDS_REVIEW"
            error = error or "上游返回完成但没有成品文件"

        return StatusResult(
            status=local,
            progress=int(data.get("progress") or (100 if local == "SUCCEEDED" else 0)),
            media_url=media_url,
            text=text,
            error=error,
            queued_ahead=int(data.get("queued_ahead") or 0),
            elapsed=data.get("elapsed"),
            raw=data if isinstance(data, dict) else {},
            done=local in ("SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_REVIEW"),
        )

    # ------------------------------------------------------------ 拉取成品
    def download(self, media_url: str, timeout: float = 300.0) -> tuple[bytes, str]:
        """把成品下载成本地字节流。绝不让浏览器直连上游地址（会泄露密钥 / 短链过期）。"""
        url = media_url
        if url.startswith("/"):
            url = self.base_url + url
        headers = {"Accept": "*/*"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = self._opener().open(req, timeout=timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError("DOWNLOAD_FAILED", "下载成品失败：%s" % exc,
                                retryable=True) from exc
        try:
            data = resp.read()
            mime = resp.headers.get("Content-Type", "") or ""
            return data, mime
        finally:
            resp.close()

    def health(self) -> dict:
        try:
            info = self._call("GET", "/healthz", timeout=6)
            return {"ok": True, "detail": info}
        except ProviderError as exc:
            return {"ok": False, "error": exc.message}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}


_REGISTRY: dict[str, VideoProvider] = {}


def get_provider(code: str = "muse", base_url: str = "", api_key: str = "") -> VideoProvider:
    key = "%s|%s|%s" % (code, base_url, bool(api_key))
    if key not in _REGISTRY:
        if code == "muse":
            _REGISTRY[key] = MuseProvider(base_url, api_key)
        else:
            raise ProviderError("UNKNOWN_PROVIDER", "未知供应商：%s" % code)
    return _REGISTRY[key]


def provider_from_row(row) -> VideoProvider:
    """从 model_providers 表构造适配器，支持后台随时改 base_url / key。"""
    return get_provider(row["code"], row["base_url"], row["api_key"])
