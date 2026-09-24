"""LLM/VLM 客户端工厂。

业务代码直接使用本模块创建 New API 客户端。
统一走 .env 配置的 new-api 中转网关，渠道分发由 new-api 后台配置。
"""

from __future__ import annotations

from wellflow.app.config import settings
from wellflow.app.llm.base import ModelRole
from wellflow.app.newapi.gateway import NewApiGateway


def _strip_provider(model: str) -> str:
    """把 "provider/xxx" 格式剥掉 provider 前缀，只保留 "xxx"。

    new-api 自己维护 provider 映射，只认短名（如 gemini-3.7-flash），
    传 provider/xxx 会 404。
    """
    if "/" in model:
        short = model.split("/", 1)[1]

        return short
    return model


def get_llm_client(
    role: ModelRole,
    *,
    model_override: str | None = None,
    max_retries: int | None = None,
) -> NewApiGateway:
    """拿到指定角色的 LLM 客户端（统一走 NewApiGateway）。

    Args:
        role: 角色 —— "vlm" 多模态识别 / "image" 图像生成 / "text" 纯文本。
        model_override: 临时覆盖模型名——Node 4 从 state 读用户选的 image_model 时用。
        max_retries: 当前客户端的额外重试次数；模型池传 0，自行接管失败切换。
    """
    if role == "image" and (not model_override or not model_override.strip()):
        raise ValueError("缺少生图模型，请先选择生图模型")
    if model_override is not None:
        model_override = model_override.strip()
    if not settings.newapi_api_key:
        raise RuntimeError(
            "NEWAPI_API_KEY 未配置，请在 .env 中设置 NEWAPI_API_KEY"
        )

    model = _strip_provider(model_override or getattr(settings, f"llm_model_{role}", "qwen3.8-flash"))
    is_image_role = (role == "image")
    timeout = settings.image_timeout if is_image_role else settings.llm_timeout

    return NewApiGateway(
        model=model,
        base_url=settings.newapi_base_url,
        api_key=settings.newapi_api_key,
        timeout=timeout,
        max_retries=max_retries,
    )
