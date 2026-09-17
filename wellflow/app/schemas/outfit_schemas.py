# -*- coding: utf-8 -*-
"""穿搭库 API 请求/响应模型(仿 asset_schemas 的模特部分风格,独立文件不碰同事文件)。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ============================================================================
# 单品与维度
# ============================================================================

class OutfitItemIn(BaseModel):
    id: str | None = None
    name: str
    category: str = ""
    color: str = ""
    storage_uri: str = ""


class OutfitDims(BaseModel):
    outfitStyle: list[str] = Field(default_factory=list)
    category: list[str] = Field(default_factory=list)
    color: list[str] = Field(default_factory=list)
    material: list[str] = Field(default_factory=list)
    fit: list[str] = Field(default_factory=list)
    func: list[str] = Field(default_factory=list)


# ============================================================================
# 入库(创建/更新)
# ============================================================================

class OutfitCreateRequest(BaseModel):
    """确认入库请求(交互端点产物全部通过 storage_uri 引用)。"""

    name: str
    desc: str | None = None
    tags: str | None = None
    scope: Literal["official", "mine"] = "mine"
    origin: Literal["upload", "url", "demo", "ai"] = "upload"
    cover_storage_uri: str | None = None       # 平铺总图
    original_storage_uri: str | None = None    # 原图
    items: list[OutfitItemIn] = Field(default_factory=list)
    dims: OutfitDims = Field(default_factory=OutfitDims)


class OutfitUpdateRequest(BaseModel):
    name: str | None = None
    desc: str | None = None
    tags: str | None = None
    cover_storage_uri: str | None = None
    original_storage_uri: str | None = None
    items: list[OutfitItemIn] | None = None
    dims: OutfitDims | None = None


# ============================================================================
# 列表 / 详情
# ============================================================================

class OutfitListItem(BaseModel):
    id: int
    outfit_no: str
    name: str
    desc: str | None = None
    tags: str | None = None
    scope: str = "mine"
    origin: str | None = None
    cover_storage_uri: str | None = None
    cover_url: str | None = None
    created_at: str = ""
    updated_at: str = ""


class OutfitListResponse(BaseModel):
    items: list[OutfitListItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


class OutfitDetailResponse(BaseModel):
    id: int
    outfit_no: str
    name: str
    desc: str | None = None
    tags: str | None = None
    scope: str = "mine"
    origin: str | None = None
    status: str = "active"
    cover_storage_uri: str | None = None
    cover_url: str | None = None
    original_storage_uri: str | None = None
    original_url: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    dims: dict[str, list[str]] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


# ============================================================================
# 交互端点:拆解 / 平铺 / 打标
# ============================================================================

class OutfitExtractRequest(BaseModel):
    """AI 拆解请求:原图通过 storage_uri 引用(先走统一上传)。"""

    original_uri: str
    session_id: str | None = None
    mode: Literal["demo", "real"] | None = None


class OutfitFlatlayRequest(BaseModel):
    """平铺合成请求:已选单品 storage_uri 列表(1-6 件)。"""

    items: list[str] = Field(..., min_length=1, max_length=6)
    session_id: str | None = None


class OutfitAutoTagRequest(BaseModel):
    """自动打标请求:读图 URI(平铺总图优先)。"""

    image_uri: str
    extra_context: str | None = None


class OutfitAutoTagResponse(BaseModel):
    dims: OutfitDims
    description: str = ""
    suggested_name: str | None = None
    model: str = ""
