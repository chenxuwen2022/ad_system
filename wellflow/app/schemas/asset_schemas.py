"""API 请求/响应的 Pydantic schema（资产域：产品 + 模特）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ============================================================================
# Brand / Series
# ============================================================================

class BrandResponse(BaseModel):
    id: int
    brand_no: str
    name: str
    summary: str | None = None
    brand_guide: str | None = None
    status: str = "active"
    series_count: int = 0
    sku_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class BrandUpdateRequest(BaseModel):
    name: str | None = None
    summary: str | None = None
    brand_guide: str | None = None
    status: str | None = None


class SeriesResponse(BaseModel):
    id: int
    series_no: str
    name: str
    summary: str | None = None
    brand_id: int
    brand_name: str = ""
    sku_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class SeriesUpdateRequest(BaseModel):
    name: str | None = None
    summary: str | None = None
    brand_id: int | None = None  # 允许迁移到另一个品牌


# ============================================================================
# SKU
# ============================================================================

class ProductImageIn(BaseModel):
    category: str = Field(..., description="正面/侧面/背面/平铺细节/下摆细节/纽扣细节/面料肌理/其他")
    storage_uri: str
    sort_order: int = 0


class ProductHistoricalAssetIn(BaseModel):
    asset_type: str  # ad_image / main_image / detail_image / other
    storage_uri: str
    source: str | None = None
    task_id: str | None = None


class SkuCreateRequest(BaseModel):
    """新建 SKU —— 同时接受 brand/series 的名称（find-or-create）或直接传 ID。"""

    # brand：name + id 二选一
    brand_id: int | None = None
    brand_name: str | None = None
    brand_guide: str | None = None  # 首次创建品牌时可选填入

    # series：name + id 二选一
    series_id: int | None = None
    series_name: str | None = None

    name: str
    style_no: str = Field(..., description="货号/款号，必填")
    category: str | None = None
    color: str | None = None
    material: str | None = None
    silhouette: str | None = None
    season: str | None = None
    selling_points: str | None = None
    brand_summary: str | None = None
    source_url: str | None = None

    images: list[ProductImageIn] = Field(default_factory=list)


class SkuUpdateRequest(BaseModel):
    name: str | None = None
    style_no: str | None = None
    category: str | None = None
    color: str | None = None
    material: str | None = None
    silhouette: str | None = None
    season: str | None = None
    selling_points: str | None = None
    brand_summary: str | None = None
    source_url: str | None = None
    status: str | None = None


class SkuListItem(BaseModel):
    id: int
    sku_no: str
    name: str
    style_no: str | None = None
    brand_name: str = ""
    series_name: str = ""
    category: str | None = None
    color: str | None = None
    status: str = "active"
    image_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class SkuListResponse(BaseModel):
    items: list[SkuListItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


class SkuImageResponse(BaseModel):
    id: int
    category: str
    storage_uri: str
    url: str = ""
    sort_order: int = 0
    created_at: str = ""


class SkuDetailResponse(BaseModel):
    id: int
    sku_no: str
    name: str
    style_no: str | None = None
    category: str | None = None
    color: str | None = None
    material: str | None = None
    silhouette: str | None = None
    season: str | None = None
    selling_points: str | None = None
    brand_summary: str | None = None
    source_url: str | None = None
    status: str = "active"
    brand_id: int
    brand_name: str = ""
    series_id: int
    series_name: str = ""
    images: list[SkuImageResponse] = Field(default_factory=list)
    image_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class NavNode(BaseModel):
    """左侧导航树节点。"""
    type: Literal["brand", "series", "sku"]
    id: int
    label: str
    count: int = 0
    children: list["NavNode"] = Field(default_factory=list)


NavNode.model_rebuild()


# ============================================================================
# 模特库
# ============================================================================

class MannequinTagIn(BaseModel):
    group_key: str  # 基础身份 / 面部 / 风格 / 使用场景
    dim_key: str    # 性别 / 年龄 / 脸型 / 模特风格 / ...
    tag_values: list[str]


# ============================================================================
# 模特创建流程 —— 交互端点 + 入库端点（全部改 multipart/form-data，无 JSON body）
# ============================================================================

class MannequinOptimizePromptResponse(BaseModel):
    final_prompt: str


class GeneratedImage(BaseModel):
    """首轮生图返回的单张 —— 不落盘，只回 base64。"""
    index: int
    base64: str


class MannequinGenerateResponse(BaseModel):
    images: list[GeneratedImage]
    model: str            # 实际使用的后端模型名
    final_prompt: str     # 最终发给模型的 prompt


class MannequinFineTuneResponse(BaseModel):
    """微调返回 —— 不落盘，只回 base64。"""
    base64: str
    model: str


class MannequinAutoTagResponse(BaseModel):
    tags: list[MannequinTagIn]        # VLM 建议的标签（前端可编辑）
    description: str                  # VLM 生成的一句话描述
    suggested_name: str | None = None  # 建议的模特名字（可选）
    model: str


class MannequinUpdateRequest(BaseModel):
    name: str | None = None
    en_name: str | None = None
    scope: Literal["official", "mine"] | None = None
    cover_storage_uri: str | None = None
    description: str | None = None
    # tags 全量替换（传完整新列表）
    tags: list[MannequinTagIn] | None = None


class MannequinListItem(BaseModel):
    id: int
    mannequin_no: str
    name: str
    en_name: str | None = None
    scope: str = "mine"
    origin: str | None = None
    status: str = "active"
    cover_storage_uri: str | None = None
    tag_summary: list[str] = Field(default_factory=list)  # 前端卡片展示的标签
    description: str | None = None
    created_at: str = ""
    updated_at: str = ""


class MannequinListResponse(BaseModel):
    items: list[MannequinListItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


class MannequinDetailResponse(BaseModel):
    id: int
    mannequin_no: str
    name: str
    en_name: str | None = None
    scope: str = "mine"
    origin: str | None = None
    status: str = "active"
    cover_storage_uri: str | None = None
    cover_url: str | None = None
    description: str | None = None
    generate_model: str | None = None
    generate_prompt: str | None = None
    tags: list[MannequinTagIn] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class MannequinDimensionsResponse(BaseModel):
    """维度枚举 —— 前端下拉菜单用。"""
    groups: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    # 例如: {
    #   "基础身份": { "性别": ["女","男"], "年龄": ["18-25",...] },
    #   "面部":    { "脸型": [...], "发型": [...] },
    # }
