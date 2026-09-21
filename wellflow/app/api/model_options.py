"""NewAPI model list proxy for frontend model selectors."""

from __future__ import annotations

from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from wellflow.app.api.utils import StandardResponse, ok
from wellflow.app.config import settings


router = APIRouter(prefix="/model-options", tags=["模型选项"])

ModelCapability = Literal["text", "vlm", "image", "video", "audio", "embedding", "rerank"]


class ModelOption(BaseModel):
    id: str
    value: str
    label: str
    provider: str = ""
    owned_by: str = ""
    capabilities: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class ModelOptionsResponse(BaseModel):
    source: str = "newapi"
    capability: str | None = None
    channel_id: int | None = None
    items: list[ModelOption] = Field(default_factory=list)


IMAGE_HINTS = ("image", "gpt-image", "dall", "flux", "sdxl", "midjourney", "mj", "banana")
VIDEO_HINTS = ("video", "kling", "sora", "veo", "wan", "hailuo", "vidu", "runway")
EMBEDDING_HINTS = ("embedding", "embed", "bge", "text-embedding")
RERANK_HINTS = ("rerank", "reranker")
AUDIO_HINTS = ("audio", "tts", "stt", "whisper", "speech", "suno")
VLM_HINTS = ("vision", "vlm", "gemini", "qwen-vl", "gpt-4o", "gpt-5")


def _short_model_name(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


def _capabilities_for(model_id: str, owned_by: str) -> list[str]:
    text = f"{model_id} {owned_by}".lower()
    caps: list[str] = []
    if any(hint in text for hint in IMAGE_HINTS):
        caps.append("image")
    if any(hint in text for hint in VIDEO_HINTS):
        caps.append("video")
    if any(hint in text for hint in EMBEDDING_HINTS):
        caps.append("embedding")
    if any(hint in text for hint in RERANK_HINTS):
        caps.append("rerank")
    if any(hint in text for hint in AUDIO_HINTS):
        caps.append("audio")
    if any(hint in text for hint in VLM_HINTS):
        caps.append("vlm")
    if not caps:
        caps.append("text")
    return caps


async def _fetch_newapi_models(channel_id: int | None = None) -> list[dict[str, Any]]:
    if not settings.newapi_api_key:
        raise RuntimeError("NEWAPI_API_KEY 未配置")
    url = f"{settings.newapi_base_url.rstrip('/')}/models"
    params: dict[str, Any] = {}
    if channel_id is not None:
        params["channel_id"] = channel_id
    try:
        async with httpx.AsyncClient(timeout=10.0, proxy=None, trust_env=False) as client:
            response = await client.get(
                url,
                params=params if params else None,
                headers={"Authorization": f"Bearer {settings.newapi_api_key}"},
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"NewAPI 模型列表请求失败: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(
            f"NewAPI /models HTTP {response.status_code}: {response.text[:300]}"
        )
    body = response.json()
    raw_items = body.get("data") if isinstance(body, dict) else None
    if not isinstance(raw_items, list):
        raise RuntimeError("NewAPI /models 响应缺少 data 数组")
    return [item for item in raw_items if isinstance(item, dict)]



async def fetch_model_options(
    capability: ModelCapability | None = None,
    *,
    channel_id: int | None = None,
) -> list[ModelOption]:
    """业务层/ModelPool 可直接复用的异步入口。

    - channel_id 非空 → 走 NewAPI admin 接口 /api/channel/{id}
      （渠道后台显式勾选的模型，与前端下拉框对齐）
    - channel_id 为空 → 走公开端点 /v1/models（该渠道可探测的全部模型）
    - capability: 按能力过滤（text / vlm / image / video / ...）

    统一入口：ModelPool 和前端 /api/model-options 都复用这里，避免两边口径分叉。
    """
    raw_items = (
        await _fetch_channel_models(channel_id)
        if channel_id is not None
        else await _fetch_newapi_models()
    )
    items = [option for item in raw_items if (option := _to_option(item))]
    if capability:
        items = [item for item in items if capability in item.capabilities]
    return items

async def _fetch_channel_models(channel_id: int) -> list[dict[str, Any]]:
    """从 NewAPI 管理接口拉取某渠道显式配置的模型列表。

    注意：返回的模型名是短名（如 qwen3.8-flash，不带 provider 前缀），
    与 /v1/models 公开端点的返回格式不同。

    抛 RuntimeError —— 调用方（ModelPool / API 端点）自己决定如何包装。
    """
    if not settings.newapi_admin_access_token:
        raise RuntimeError("NEWAPI_ADMIN_ACCESS_TOKEN 未配置，无法访问 NewAPI 管理接口")

    url = f"{settings.newapi_admin_base_url.rstrip('/')}/api/channel/{channel_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {settings.newapi_admin_access_token}"},
            )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"NewAPI 渠道模型列表请求失败: {exc}") from exc

    channel = (
        body.get("data")
        if isinstance(body, dict) and body.get("success") is True
        else None
    )
    models = channel.get("models") if isinstance(channel, dict) else None
    if not isinstance(models, str):
        raise RuntimeError("NewAPI 渠道模型列表格式错误")

    names = dict.fromkeys(name.strip() for name in models.split(",") if name.strip())
    return [{"id": name} for name in names]


def _to_option(item: dict[str, Any]) -> ModelOption | None:
    model_id = item.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    owned_by = item.get("owned_by")
    owned_by_str = owned_by if isinstance(owned_by, str) else ""
    provider = model_id.split("/", 1)[0] if "/" in model_id else owned_by_str
    short = _short_model_name(model_id)
    return ModelOption(
        id=short,
        value=model_id,
        label=short,
        provider=provider,
        owned_by=owned_by_str,
        capabilities=_capabilities_for(model_id, owned_by_str),
        raw=item,
    )


@router.get("", response_model=StandardResponse[ModelOptionsResponse], summary="获取 NewAPI 可用模型列表")
async def list_model_options(
    capability: ModelCapability | None = Query(default=None, description="按能力过滤，如 image / video / text / vlm"),
    model_type: ModelCapability | None = Query(
        default=None,
        alias="type",
        description="capability 的别名，如 image / video / text / vlm",
    ),
    channel_id: int | None = Query(
        default=None,
        gt=0,
        description="New API 渠道 ID（非空时走 admin 接口，与 ModelPool 对齐）",
    ),
):
    selected_capability = capability or model_type
    try:
        items = await fetch_model_options(selected_capability, channel_id=channel_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ok(
        ModelOptionsResponse(
            source="newapi-channel" if channel_id is not None else "newapi",
            capability=selected_capability,
            channel_id=channel_id,
            items=items,
        )
    )
