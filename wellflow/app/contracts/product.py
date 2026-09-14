"""商品识别与调研相关的结构化数据契约（final.md 第 4.1 节 + Node 1）。"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Evidence(str, Enum):
    """证据来源枚举。禁止把推测伪装成事实。"""

    OBSERVED = "observed"    # 图像观察到的事实
    PROVIDED = "provided"    # 用户直接提供的规格
    INFERRED = "inferred"    # 推理/归纳得到的不确定结论
    UNKNOWN = "unknown"      # 无法确认


class EvidenceField(BaseModel):
    """带证据和置信度的字段值。"""

    value: str | list[str]
    evidence: Evidence = Evidence.OBSERVED
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)


class InputAnalysis(BaseModel):
    """InputAnalyzer 输出：输入可用性分析。"""

    has_images: bool
    has_text: bool
    image_count: int = 0
    needs_preprocessing: bool = False
    preprocessing_reasons: list[str] = Field(default_factory=list)
    quality_score: float = Field(ge=0.0, le=1.0, default=1.0)


class AssetRef(BaseModel):
    """可溯源的外部资产引用。"""

    asset_id: str
    asset_type: Literal["input", "preprocessed", "generated"]
    storage_uri: str
    mime_type: str = "image/jpeg"
    source_ref: str | None = None  # 溯源：从哪个原始资产派生


class ProductProfile(BaseModel):
    """ProductAnalyzer 输出的商品属性画像。每一项都必须带证据。"""

    category: EvidenceField | None = None
    material: EvidenceField | None = None
    color: EvidenceField | None = None
    silhouette: EvidenceField | None = None
    craftsmanship: EvidenceField | None = None
    details: EvidenceField | None = None
    functional_features: EvidenceField | None = None
    style_tags: list[str] = Field(default_factory=list)
    unknown_fields: list[str] = Field(default_factory=list)


class ResearchInsight(BaseModel):
    """ResearchAgent 调研输出。"""

    market_positioning: str = ""
    target_audience: list[str] = Field(default_factory=list)
    competitor_themes: list[str] = Field(default_factory=list)
    trend_signals: list[str] = Field(default_factory=list)
    selling_point_opportunities: list[str] = Field(default_factory=list)
    platform_suggestions: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    evidence_insufficient: bool = False


class ProductInsight(BaseModel):
    """Node 1 最终输出：ProductProfile + ResearchInsight 的合并。"""

    profile: ProductProfile
    research: ResearchInsight
    raw_text_summary: str = ""


class C1Confirmation(BaseModel):
    """C1 用户确认数据。"""

    confirmed_profile: ProductProfile
    confirmed_audience_tags: list[str] = Field(default_factory=list)
    confirmed_selling_point_tags: list[str] = Field(default_factory=list)
    notes: str = ""
