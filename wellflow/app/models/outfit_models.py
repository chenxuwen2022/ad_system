# -*- coding: utf-8 -*-
"""穿搭库 ORM 模型(仿 mannequin_models:主表 + JSONB 嵌套结构)。

穿搭维度的六组标签(outfitStyle/category/color/material/fit/func)是平铺数组,
单品清单(items)是嵌套数组 —— 两者都用 JSONB 直接存主表,不需要像
模特库那样拆独立标签表(维度口径不同,这是合理简化)。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BIGINT, DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from wellflow.app.database import Base


class Outfit(Base):
    """穿搭资产。"""

    __tablename__ = "outfit"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    outfit_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)  # WF-O001
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    desc: Mapped[str | None] = mapped_column(Text, nullable=True)            # 一句话素材描述
    tags: Mapped[str | None] = mapped_column(String(512), nullable=True)     # 逗号分隔(搜索/卡片文案)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="mine")  # official/mine
    origin: Mapped[str | None] = mapped_column(String(32), nullable=True)    # upload/url/demo/ai
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    cover_storage_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)     # 平铺总图
    original_storage_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)  # 原图
    items: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # [{id,name,category,color,storage_uri}]
    dims: Mapped[dict | None] = mapped_column(JSONB, nullable=True)   # {outfitStyle:[..],category:[..],color:[..],material:[..],fit:[..],func:[..]}
    owner_id: Mapped[int | None] = mapped_column(BIGINT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_outfit_scope", "scope", "status"),
        Index("ix_outfit_no", "outfit_no"),
    )
