"""模特库 ORM 模型（mannequin / tag / generate_log）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BIGINT, ForeignKey, String, Text, Integer, DateTime, Index, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wellflow.app.database import Base


# ============================================================================
# 模特 Mannequin
# ============================================================================

class Mannequin(Base):
    """模特资产。"""

    __tablename__ = "mannequin"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    mannequin_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)  # WF-M001
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    en_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="mine")  # official/mine
    origin: Mapped[str | None] = mapped_column(String(32), nullable=True)          # upload/ai_generate
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    cover_storage_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)           # AI 合成素材描述
    generate_model: Mapped[str | None] = mapped_column(String(64), nullable=True)  # AI 图像生成模型名
    generate_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[int | None] = mapped_column(BIGINT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    tags: Mapped[list["MannequinTag"]] = relationship(
        "MannequinTag", back_populates="mannequin", cascade="all, delete-orphan", lazy="selectin",
    )

    __table_args__ = (
        Index("ix_mannequin_scope", "scope", "status"),
        Index("ix_mannequin_owner", "owner_id"),
    )


class MannequinTag(Base):
    """模特维度标签（分组多值）。"""

    __tablename__ = "mannequin_tag"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    mannequin_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("mannequin.id", ondelete="CASCADE"), nullable=False,
    )
    group_key: Mapped[str] = mapped_column(String(32), nullable=False)  # 基础身份/面部/风格/使用场景
    dim_key: Mapped[str] = mapped_column(String(32), nullable=False)    # 性别/年龄/脸型/...
    tag_value: Mapped[str] = mapped_column(String(64), nullable=False)  # 女/18-25/方脸/...

    mannequin: Mapped[Mannequin] = relationship("Mannequin", back_populates="tags")

    __table_args__ = (
        UniqueConstraint("mannequin_id", "dim_key", "tag_value", name="uk_mannequin_tag"),
        Index("ix_mannequin_tag_id", "mannequin_id"),
        Index("ix_mannequin_tag_filter", "dim_key", "tag_value"),
        Index("ix_mannequin_tag_group", "group_key"),
    )


class MannequinGenerateLog(Base):
    """模特 AI 生成历史。"""

    __tablename__ = "mannequin_generate_log"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    mannequin_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("mannequin.id", ondelete="SET NULL"), nullable=True,
    )
    input_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_refs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)   # 参考图 URI 列表
    final_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    generate_model: Mapped[str | None] = mapped_column(String(64), nullable=True)  # AI 模型名
    num_output: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    output_uris: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # 输出图 URI 列表
    status: Mapped[str] = mapped_column(String(16), nullable=False)          # pending/success/failed
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_mannequin_gen_id", "mannequin_id"),
        Index("ix_mannequin_gen_time", "created_at"),
    )
