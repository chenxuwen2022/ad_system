# -*- coding: utf-8 -*-
"""场景库 ORM 模型(仿 outfit_models:主表 + JSONB 五维标签 + 路径入库)。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BIGINT, DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from wellflow.app.database import Base


class Scene(Base):
    """场景资产(商拍「环境参考」素材)。"""

    __tablename__ = "scene"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    scene_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)  # WF-S001
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    desc: Mapped[str | None] = mapped_column(Text, nullable=True)        # 一句话素材描述
    tags: Mapped[str | None] = mapped_column(String(512), nullable=True)  # 逗号分隔(搜索/卡片文案)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="mine")  # official/mine
    origin: Mapped[str | None] = mapped_column(String(32), nullable=True)  # upload/url/ai
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="extracting")
    cover_storage_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)     # 场景图(处理成果)
    original_storage_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)  # 上传原图
    mosaic_storage_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)    # 原图马赛克版(人+违规物打码)
    dims: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # {space:[..],region:[..],season:[..],weather:[..],sceneStyle:[..]}
    owner_id: Mapped[int | None] = mapped_column(BIGINT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_scene_scope", "scope", "status"),
        Index("ix_scene_no", "scene_no"),
    )
