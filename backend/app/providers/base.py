"""供应商适配器契约（对齐 V3 文档 §8）。

任何厂商接入只要实现这 6 个方法，上层任务引擎、计费、前端都不用改。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GenRequest:
    prompt: str
    kind: str = "video"          # video / image
    model: str = "muse-video"
    duration: int = 10
    aspect_ratio: str = "9:16"
    resolution: str = "1080p"
    image_url: str = ""          # 参考图（data URL 或公网 URL）
    thread_key: str = ""         # 号池侧会话标识，保证同一用户同一对话承接上下文
    reuse_thread: bool = False
    extra: str = ""
    timeout: int = 600
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubmitResult:
    provider_task_id: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class StatusResult:
    status: str                  # queued / processing / completed / failed / needs_review
    progress: int = 0
    media_url: str = ""
    text: str = ""
    error: str = ""
    queued_ahead: int = 0
    elapsed: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    done: bool = False


class ProviderError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class VideoProvider(ABC):
    code: str = "base"
    name: str = "Base"

    @abstractmethod
    def capabilities(self) -> dict:
        """支持的时长、比例、分辨率、首帧、音频、回调、取消、价格、地区。"""

    @abstractmethod
    def estimate_cost(self, req: GenRequest) -> dict:
        """返回 {points, cost_minor}。"""

    @abstractmethod
    def submit(self, req: GenRequest, idempotency_key: str) -> SubmitResult:
        ...

    @abstractmethod
    def get_status(self, provider_task_id: str, kind: str = "video") -> StatusResult:
        ...

    def cancel(self, provider_task_id: str) -> bool:
        """不支持取消时返回 False，由上层按规则处理（保留积分待人工对账）。"""
        return False

    @abstractmethod
    def health(self) -> dict:
        ...
