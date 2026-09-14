"""LLM/VLM 客户端工厂。

业务层只通过这个模块拿客户端，不 import 具体网关实现。
所有请求统一走 new-api 中转网关（局域网），由 new-api 按模型名路由到真实后端。
"""

from __future__ import annotations

from typing import Literal

from wellflow.app.config import settings
from wellflow.app.llm.base import BaseLLMClient


ModelRole = Literal["vlm", "image"]


def _resolve_model(role: ModelRole) -> str:
    """根据 role 拿到实际模型名，值来自 config.py Settings.llm_model_xxx（含默认值）。"""
    return getattr(settings, f"llm_model_{role}")


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


def get_llm_client(role: ModelRole, *, model_override: str | None = None) -> BaseLLMClient:
    """拿到指定角色的 LLM 客户端。

    路由规则（统一走 new-api 中转网关）：
    ┌─────────┬───────────────────────────────────────────────────┐
    │ vlm     │ OpenAI 协议 → OfoxGateway（/v1/chat/completions）│
    │ image   │ multipart → LaozhangGateway（/v1/images/edits） │
    └─────────┴───────────────────────────────────────────────────┘

    Args:
        role: 角色 —— "vlm" 多模态识别 / "image" 图像生成。
        model_override: 临时覆盖模型名——Node 3 从 state 读用户在前端选的 image_model 时用。
    """
    if not settings.newapi_api_key:
        raise RuntimeError("newapi_api_key 未配置，请在 .env 中设置 NEWAPI_API_KEY")

    model = _strip_provider(model_override or _resolve_model(role))
    newapi_base = settings.newapi_base_url
    newapi_key = settings.newapi_api_key

    if role == "image":
        from wellflow.app.llm.laozhang_gateway import LaozhangGateway

        print(f"[factory] → image → LaozhangGateway (new-api) model={model}", flush=True)
        return LaozhangGateway(
            base_url=newapi_base,
            api_key=newapi_key,
            model=model,
            timeout=settings.image_timeout,
            proxy_url=None,  # new-api 在局域网，不走代理
        )

    # vlm / 其他 role
    from wellflow.app.llm.ofox_gateway import OfoxGateway

    print(f"[factory] → vlm → OfoxGateway (new-api) model={model}", flush=True)
    return OfoxGateway(
        model=model,
        base_url=newapi_base,
        api_key=newapi_key,
        timeout=settings.llm_timeout,
        proxy_url=None,  # new-api 在局域网，不走代理
    )
