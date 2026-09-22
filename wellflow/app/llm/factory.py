"""LLM/VLM 客户端工厂。

业务层只通过这个模块拿客户端，不 import 具体网关实现。
统一走 .env 配置的 new-api 中转网关，渠道分发由 new-api 后台配置。
"""

from __future__ import annotations

from typing import Literal

from wellflow.app.config import settings
from wellflow.app.llm.base import BaseLLMClient


ModelRole = Literal["vlm", "image", "text"]


def _resolve_model(role: ModelRole) -> str:
    """根据 role 拿到模型名。vlm / text 走 ModelPool 动态获取，image / responses 等按配置。"""
    return getattr(settings, f"llm_model_{role}", "qwen3.8-flash")


def _strip_provider(model: str) -> str:
    """把 "provider/xxx" 格式剥掉 provider 前缀，只保留 "xxx"。

    new-api 自己维护 provider 映射，只认短名（如 gpt-image-2 / gemini-3.7-flash），
    传 provider/xxx 会 404。
    """
    if "/" in model:
        short = model.split("/", 1)[1]
        print(f"[factory] 🔧 模型名规范化: '{model}' → '{short}'（去掉 provider 前缀）", flush=True)
        return short
    return model


def get_llm_client(
    role: ModelRole,
    *,
    model_override: str | None = None,
) -> BaseLLMClient:
    """拿到指定角色的 LLM 客户端（统一走 NewApiGateway）。

    Args:
        role: 角色 —— "vlm" 多模态识别 / "image" 图像生成 / "text" 纯文本。
        model_override: 临时覆盖模型名——Node 4 从 state 读用户选的 image_model 时用。
    """
    if not settings.newapi_api_key:
        raise RuntimeError(
            "NEWAPI_API_KEY 未配置，请在 .env 中设置 NEWAPI_API_KEY"
        )

    model = _strip_provider(model_override or _resolve_model(role))
    is_image_role = (role == "image")
    timeout = settings.image_timeout if is_image_role else settings.llm_timeout

    from wellflow.app.llm.newapi_gateway import NewApiGateway
    print(f"[factory] 🔵 NewApiGateway model={model} role={role}", flush=True)
    return NewApiGateway(
        model=model,
        base_url=settings.newapi_base_url,
        api_key=settings.newapi_api_key,
        timeout=timeout,
        proxy_url=None,  # 网关直连，不走本机代理
    )
