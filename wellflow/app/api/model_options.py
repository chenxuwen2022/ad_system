"""供前端模型选择器使用的 HTTP 接口。"""

from __future__ import annotations

from wellflow.app.logging import page_context

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from wellflow.app.api.utils import StandardResponse, ok
from wellflow.app.newapi.catalog import ModelCapability, ModelOption, fetch_model_options

router = APIRouter(prefix="/model-options", tags=["模型选项"])


class ModelOptionsResponse(BaseModel):
    source: str = "newapi"
    capability: str | None = None
    channel_id: int | None = None
    items: list[ModelOption] = Field(default_factory=list)


@router.get("", response_model=StandardResponse[ModelOptionsResponse], summary="获取 NewAPI 可用模型列表")
@page_context('模型配置')
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
