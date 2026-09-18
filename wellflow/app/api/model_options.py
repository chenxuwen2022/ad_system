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


async def _fetch_newapi_models() -> list[dict[str, Any]]:
    if not settings.newapi_api_key:
        raise HTTPException(status_code=503, detail="NEWAPI_API_KEY 未配置")
    url = f"{settings.newapi_base_url.rstrip('/')}/models"
    try:
        async with httpx.AsyncClient(timeout=10.0, proxy=None, trust_env=False) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {settings.newapi_api_key}"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"NewAPI 模型列表请求失败: {exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"NewAPI /models HTTP {response.status_code}: {response.text[:300]}",
        )
    body = response.json()
    raw_items = body.get("data") if isinstance(body, dict) else None
    if not isinstance(raw_items, list):
        raise HTTPException(status_code=502, detail="NewAPI /models 响应缺少 data 数组")
    return [item for item in raw_items if isinstance(item, dict)]


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
):
    selected_capability = capability or model_type
    raw_items = await _fetch_newapi_models()
    items = [option for item in raw_items if (option := _to_option(item))]
    if selected_capability:
        items = [item for item in items if selected_capability in item.capabilities]
    return ok(ModelOptionsResponse(capability=selected_capability, items=items))
