"""Immutable prompt revisions and category release snapshots."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from wellflow.app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class PromptTemplate(Base):
    __tablename__ = "prompt_template"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    draft_revision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PromptRevision(Base):
    __tablename__ = "prompt_revision"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_key: Mapped[str] = mapped_column(ForeignKey("prompt_template.key"), nullable=False, index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("template_key", "number"),)


class PromptRelease(Base):
    __tablename__ = "prompt_release"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("category", "number"),)


class PromptReleaseItem(Base):
    __tablename__ = "prompt_release_item"
    release_id: Mapped[int] = mapped_column(ForeignKey("prompt_release.id", ondelete="CASCADE"), primary_key=True)
    template_key: Mapped[str] = mapped_column(ForeignKey("prompt_template.key"), primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("prompt_revision.id"), nullable=False)
