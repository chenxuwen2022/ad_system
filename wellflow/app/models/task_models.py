"""任务域 ORM 模型（Task / TaskEvent / TaskErrorLog / TaskImage）。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, String, Text, JSON, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wellflow.app.database import Base


class TaskPhase(str, Enum):
    INPUT = "input"
    RESEARCH = "research"
    C1_CONFIRM = "c1_confirm"
    PLANNING = "planning"
    C2_CONFIRM = "c2_confirm"
    DELIVERY = "delivery"
    DONE = "done"
    FAILED = "failed"
    NEEDS_RETRY = "needs_retry"


class Task(Base):
    """任务主表。"""

    __tablename__ = "task"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    phase: Mapped[str] = mapped_column(String(32), default=TaskPhase.INPUT.value)
    request_json: Mapped[dict] = mapped_column(JSON, default=dict)
    brand_config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    selected_plan_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    interrupt_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TaskEvent(Base):
    """任务事件审计表。"""

    __tablename__ = "task_event"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task.task_id"), index=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64))  # phase_change / llm_call / skill_call / qa / human_review ...
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[float | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TaskErrorLog(Base):
    """任务错误日志。"""

    __tablename__ = "task_error_log"

    error_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task.task_id"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(128), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(default=False)
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TaskImage(Base):
    """任务关联图片（输入模特图 + 最终生图成品）。

    统一存「图片文件路径 + 元数据」，字节落盘在 uploads/，DB 只存 storage_uri。
    image_type: "model"（C1 上传的模特图）| "output"（确认结束后的生图成品）。
    """

    __tablename__ = "task_image"

    image_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task.task_id"), index=True)
    image_type: Mapped[str] = mapped_column(String(16))  # model | output
    storage_uri: Mapped[str] = mapped_column(String(512))
    shot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)   # output 用，= work_item_id
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)          # output 用
    prompt_index: Mapped[int | None] = mapped_column(Integer, nullable=True) # output 用
    variant_index: Mapped[int | None] = mapped_column(Integer, nullable=True)# output 用
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
