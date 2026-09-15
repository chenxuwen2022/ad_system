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


class MannequinCreateRequest(BaseModel):
    """最终入库：上传方式或 AI 生成方式共用一个 schema，由 origin 区分。"""

    name: str
    en_name: str | None = None
    scope: Literal["official", "mine"] = "mine"
    origin: Literal["upload", "ai_generate"] = "upload"
    cover_storage_uri: str
    description: str | None = None
    tags: list[MannequinTagIn] = Field(default_factory=list)

    # AI 生成上下文（写 generate_log 用；路径 A 时这些全为 None）
    input_desc: str | None = None
    input_refs: list[str] = Field(default_factory=list)
    final_prompt: str | None = None
    generate_model: str | None = None
    num_output: int = 1
    fine_tune_from: str | None = None   # 微调前那一轮的图 URI（如果走了微调）
    fine_tune_prompt: str | None = None


# ============================================================================
# 模特创建流程 —— 交互端点（无状态）
# ============================================================================

class UploadedImage(BaseModel):
    """上传返回的单张图片信息。"""
    storage_uri: str  # 相对路径，如 uploads/mne_xxx/m0.jpg
    url: str          # 前端直接可访问的 URL
    filename: str


class MannequinOptimizePromptRequest(BaseModel):
    """路径B「优化提示词」按钮入参。"""
    raw_prompt: str                                    # 用户输入的原始提示词
    ref_uris: list[str] = Field(default_factory=list)  # 路径A上传的参考图（可选）
    tags: list[MannequinTagIn] = Field(default_factory=list)  # 用户选的维度标签（可空）

class MannequinOptimizePromptResponse(BaseModel):
    final_prompt: str                                  # 优化后的最终提示词


class GeneratedImage(BaseModel):
    """首轮生图返回的单张。"""
    index: int
    base64: str
    storage_uri: str | None = None       # 后端可选择先不落盘，让前端决定
    url: str | None = None
    revised_prompt: str | None = None


class MannequinGenerateRequest(BaseModel):
    """首轮生图 / 批量生成：n 张。"""
    session_id: str                                    # upload 返回的 session_id，用于组织落盘目录
    prompt: str                                        # 最终提示词（可能已优化）
    ref_uris: list[str] = Field(default_factory=list)  # 路径A上传的参考图（可选）
    tags: list[MannequinTagIn] = Field(default_factory=list)
    generate_model: str                                # 前端模型名，后端会映射
    num_output: int = Field(default=4, ge=1, le=6)
    size: str = "1024x1536"

class MannequinGenerateResponse(BaseModel):
    images: list[GeneratedImage]
    model: str            # 实际使用的后端模型名
    final_prompt: str     # 最终发给模型的 prompt


class MannequinFineTuneRequest(BaseModel):
    """对选中的一张图做图生图微调（保持身份一致性）。"""
    session_id: str                                    # upload 返回的 session_id，用于组织落盘目录
    target_uri: str                                     # 选中的那张图的 URI
    tune_prompt: str                                    # 微调描述，如"让头发更长一些"
    original_prompt: str | None = None                  # 原始生图 prompt，用于维持身份
    ref_uris: list[str] = Field(default_factory=list)   # 原路径A参考图（可选保留）
    generate_model: str
    size: str = "1024x1536"

class MannequinFineTuneResponse(BaseModel):
    base64: str
    storage_uri: str | None = None
    url: str | None = None
    model: str
    revised_prompt: str | None = None


class MannequinAutoTagRequest(BaseModel):
    """VLM 读图，根据 15 维度枚举自动给出标签建议。"""
    image_uri: str                     # 最终入库的那张图（路径A原图 or 路径B微调/选中图）
    extra_context: str | None = None   # 用户输入的提示词/描述，帮助 VLM 理解意图

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
