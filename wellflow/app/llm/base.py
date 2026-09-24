"""LLM/VLM 网关抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal


ModelRole = Literal["vlm", "image", "text"]


class LLMResponse:
    """统一的 LLM 响应。"""

    def __init__(
        self,
        content: str,
        raw: Any = None,
        usage: dict[str, int] | None = None,
        model: str = "",
        latency_ms: float | None = None,
        thinking: str | None = None,
    ):
        self.content = content
        self.raw = raw
        self.usage = usage or {}
        self.model = model
        self.latency_ms = latency_ms
        self.thinking = thinking  # 非流式响应中的推理/思考文本（如果有）


class ImageGenResult:
    """图像生成统一返回。

    单张: url / b64_json 直接赋值，variants=None
    批量（n>1）: variants 列表存每张子图，主 url/b64_json 取第一张（兼容旧代码）
    """

    def __init__(
        self,
        url: str | None = None,          # 远程 URL
        b64_json: str | None = None,     # base64 编码（不含 data: 前缀）
        raw: Any = None,
        model: str = "",
        variants: list["ImageGenResult"] | None = None,  # n>1 时存放所有子图
    ):
        self.url = url
        self.b64_json = b64_json
        self.raw = raw
        self.model = model
        self.variants = variants

    @property
    def data_uri(self) -> str | None:
        """如果有 b64_json，返回可直接用于前端 <img src> 的 data URI。"""
        if self.b64_json:
            return f"data:image/png;base64,{self.b64_json}"
        return None

    @property
    def all_images(self) -> list["ImageGenResult"]:
        """统一入口：返回本实例（单张）或 variants（批量）。"""
        if self.variants:
            return self.variants
        return [self]


class InsufficientCreditsError(RuntimeError):
    """上游支付额度不足（HTTP 402 / insufficient_credits）。

    所有调用链最终会被 API 层捕获，返回给前端 HTTP 402 + 上游原始 message，
    而不是笼统的 502 / 全部生图失败。
    """

    def __init__(self, upstream_message: str = "上游账户额度不足，请联系管理员充值"):
        super().__init__(upstream_message)
        self.upstream_message = upstream_message


class ImageRateLimitError(RuntimeError):
    """明确被拒绝的生图请求；只由共享生图服务负责限流重试。"""

    def __init__(self, message: str, retry_after: str | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def extract_error_message(status_code: int, text: str) -> str:
    """从网关错误响应里提取干净的 message，供前端原封不动展示。

    常见结构：{"error":{"message":"...","type":"..."}} 或 {"detail":"..."}。
    解析失败就回退到原始文本。
    """
    import json

    raw = (text or "").strip()
    try:
        body = json.loads(raw)
    except Exception:
        return raw or f"HTTP {status_code}"

    err = body.get("error")
    if isinstance(err, dict):
        for key in ("message", "detail", "msg", "title"):
            val = err.get(key)
            if val:
                return str(val)

    for key in ("message", "detail", "msg"):
        val = body.get(key)
        if val:
            return str(val)

    return raw or f"HTTP {status_code}"


class BaseLLMClient(ABC):
    """统一的 LLM 客户端接口。所有网关实现必须遵守这个契约。"""

    @abstractmethod
    async def chat(
        self,
        system: str,
        user: str,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.3,
        reasoning_effort: str = "close",
        extra_params: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """纯文本/结构化输出。

        Args:
            reasoning_effort: 推理/思考强度控制，可选值 "close" / "low" / "medium" / "high"。
                "close" 表示强制关闭思考；不允许传 None（必须显式指定）。
            extra_params: 透传到 payload 的额外扩展字段，供网关识别模型特定参数。
        """
        ...

    @abstractmethod
    async def chat_with_images(
        self,
        system: str,
        user: str,
        image_uris: list[str],
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str = "close",
        extra_params: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """多模态 VLM 调用（完整响应）。"""
        ...

    @abstractmethod
    async def stream_chat_with_images(
        self,
        system: str,
        user: str,
        image_uris: list[str],
        reasoning_effort: str = "close",
        extra_params: dict[str, Any] | None = None,
    ) -> Any:
        """多模态 VLM 流式调用。

        Returns:
            异步迭代器，每次 yield {"type": "thinking"|"content", "text": "..."}。
        """
        ...

    @abstractmethod
    async def stream_chat(
        self,
        system: str,
        user: str,
        reasoning_effort: str = "close",
        response_format: dict[str, Any] | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> Any:
        """纯文本流式调用（不含图片）。

        Returns:
            异步迭代器，每次 yield {"type": "thinking"|"content", "text": "..."}。
        """
        ...

    async def generate_image(
        self,
        prompt: str,
        *,
        image_uris: list[str] | None = None,
        size: str = "1024x1536",
        n: int = 1,
        response_format: str = "b64_json",
        extra_params: dict[str, Any] | None = None,
    ) -> ImageGenResult:
        """图像生成；由具体连接实现选择协议和端点。"""
        raise NotImplementedError("当前连接方式不支持图像生成")

    def langchain_compat(self):
        """由具体连接实现提供 LangChain 兼容对象。"""
        raise NotImplementedError("当前连接方式不支持 LangChain")
