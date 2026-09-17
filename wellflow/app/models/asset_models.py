"""商品域 ORM 模型（品牌 / 系列 / SKU / 图片 / 历史素材 / 知识库关联）。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BIGINT, ForeignKey, String, Text, Integer, DateTime, Index, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wellflow.app.database import Base


# ============================================================================
# 品牌 Brand
# ============================================================================

class ProductBrand(Base):
    """商品品牌（SKU 创建时 find-or-create，不暴露独立 POST 接口）。"""

    __tablename__ = "product_brand"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    brand_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    brand_guide: Mapped[str | None] = mapped_column(Text, nullable=True)   # 品牌调性/表达要求
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    series_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 冗余
    sku_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)     # 冗余
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_product_brand_status", "status"),
    )


# ============================================================================
# 系列 Series
# ============================================================================

class ProductSeries(Base):
    """商品系列（挂在品牌下，SKU 创建时 find-or-create）。"""

    __tablename__ = "product_series"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    brand_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("product_brand.id", ondelete="RESTRICT"), nullable=False,
    )
    series_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    sku_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 冗余
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    brand: Mapped[ProductBrand] = relationship("ProductBrand", lazy="joined")

    __table_args__ = (
        Index("ix_product_series_brand", "brand_id"),
    )


# ============================================================================
# SKU
# ============================================================================

class ProductSku(Base):
    """SKU 主表（唯一创建入口）。"""

    __tablename__ = "product_sku"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    sku_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    series_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("product_series.id", ondelete="RESTRICT"), nullable=False,
    )
    brand_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("product_brand.id", ondelete="RESTRICT"), nullable=False,
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    style_no: Mapped[str] = mapped_column(String(64), nullable=False)             # 货号/款号
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)       # 商品类目
    color: Mapped[str | None] = mapped_column(String(32), nullable=True)
    material: Mapped[str | None] = mapped_column(String(64), nullable=True)
    silhouette: Mapped[str | None] = mapped_column(String(32), nullable=True)    # 版型
    season: Mapped[str | None] = mapped_column(String(32), nullable=True)        # 适用季节
    selling_points: Mapped[str | None] = mapped_column(Text, nullable=True)      # 长期核心卖点
    brand_summary: Mapped[str | None] = mapped_column(Text, nullable=True)        # 品牌要求摘要
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    brand: Mapped[ProductBrand] = relationship("ProductBrand", lazy="joined")
    series: Mapped[ProductSeries] = relationship("ProductSeries", lazy="joined")
    images: Mapped[list["ProductImage"]] = relationship(
        "ProductImage", back_populates="sku", cascade="all, delete-orphan", lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("series_id", "style_no", "name", name="uk_product_sku_series_style_name"),
        Index("ix_product_sku_series", "series_id"),
        Index("ix_product_sku_brand", "brand_id"),
        Index("ix_product_sku_status", "status"),
    )


class ProductImage(Base):
    """SKU 商品图片（多角度分类）。"""

    __tablename__ = "product_image"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    sku_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("product_sku.id", ondelete="CASCADE"), nullable=False,
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)  # 正面/侧面/背面/...
    storage_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    sku: Mapped[ProductSku] = relationship("ProductSku", back_populates="images")

    __table_args__ = (
        Index("ix_product_image_sku", "sku_id"),
        Index("ix_product_image_cat", "sku_id", "category"),
    )


class ProductHistoricalAsset(Base):
    """SKU 历史素材（投流图/主图/商详图/其他）。"""

    __tablename__ = "product_historical_asset"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    sku_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("product_sku.id", ondelete="CASCADE"), nullable=False,
    )
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)  # ad_image/main_image/...
    storage_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_product_hist_asset_sku", "sku_id", "asset_type"),
    )


class ProductKnowledgeLink(Base):
    """SKU ↔ 知识库文档关联。"""

    __tablename__ = "product_knowledge_link"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    sku_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("product_sku.id", ondelete="CASCADE"), nullable=False,
    )
    doc_type: Mapped[str] = mapped_column(String(16), nullable=False)  # knowledge/chat_doc
    doc_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    match_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("sku_id", "doc_type", "doc_ref", name="uk_product_knowledge_link"),
        Index("ix_product_knowledge_sku", "sku_id"),
    )
