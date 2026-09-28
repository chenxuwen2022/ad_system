# -*- coding: utf-8 -*-
"""穿搭库 API 请求/响应模型(仿 asset_schemas 的模特部分风格,独立文件不碰同事文件)。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator


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
    status: Literal["active", "pending_select"] | None = None
    # "active"=确认入库;"pending_select"=确认页「返回上一步」退回重新选件


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
    status: str = "active"      # extracting/pending_select/generating/pending_confirm/active/failed
    cover_storage_uri: str | None = None
    cover_url: str | None = None
    original_storage_uri: str | None = None
    original_url: str | None = None
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

ImageModel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class OutfitExtractRequest(BaseModel):
    """AI 拆解请求:原图通过 storage_uri 引用(先走统一上传)。

    outfit_id:传了=对已有待选件行「重新拆解」(清单品、转 extracting,不新建);
    不传=新建行(点击即入库)。
    """

    original_uri: str
    image_model: ImageModel
    session_id: str | None = None
    mode: Literal["demo", "real"] | None = None
    outfit_id: int | None = None


class OutfitGenerateRequest(BaseModel):
    """一键生成穿搭请求:点击「生成穿搭图」立即入库(generating),后台补全。

    mode=items:传拆解好的单品清单(含抠图 storage_uri),后台只跑平铺+打标;
    mode=auto:只传原图,后台全链路(识别→抠图→平铺→打标)。
    outfit_id:传了=更新已有拆解行(选件后生成,不新建);不传=新建行。
    """

    mode: Literal["items", "auto"] = "items"
    image_model: ImageModel | None = None
    original_uri: str                       # 原图 storage_uri(先走统一上传)
    items: list[OutfitItemIn] = Field(default_factory=list)   # mode=items 必传
    outfit_id: int | None = None            # 选件后生成:更新该行,不新建
    session_id: str | None = None
    name: str | None = None                 # 可选;不传用临时名,完成后 VLM 建议名兜底
    scope: Literal["official", "mine"] = "mine"
    extra_context: str | None = None        # 打标补充意图(透传给 VLM)

    @model_validator(mode="after")
    def require_auto_image_model(self):
        if self.mode == "auto" and not self.image_model:
            raise ValueError("自动生成穿搭必须选择生图模型")
        return self


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
