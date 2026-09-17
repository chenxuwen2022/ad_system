"""任务域 ORM 模型（Task / TaskEvent / TaskErrorLog / TaskImage / Conversation / ChatMessage）。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, String, Text, JSON, Integer, Boolean
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
    """任务主表（一次 LangGraph workflow 执行 = 一条 Task）。"""

    __tablename__ = "task"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 关联的 conversation（可选：旧 task 没有，conversation-centric 后必填）
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation.conversation_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
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


# =============================================================================
# Conversation / ChatMessage — 会话持久化（Phase 1）
# =============================================================================


class Conversation(Base):
    """会话（左侧历史的一行 = 一个 conversation，内部可含多个 Task）。

    Conversation-centric 后：
      - 用户首次发消息 → 自动新建一个 conversation
      - 用户在已有 conversation 里继续发消息 → 复用，可能触发新 task（重做/新商品）
      - task 表加 conversation_id FK，一个 conversation 可挂多个 task
    """

    __tablename__ = "conversation"

    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(128), default="新对话")
    # 可选：当前活跃 task（用于前端恢复时直接跳转到）
    current_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatMessage(Base):
    """conversation 内的一条消息（自由文本 + 可选图片）。

    与 workflow 节点产物（node1/2/3/4）不同——后者存 LangGraph checkpoint，
    本表只存"用户/助手在对话里说过的话"，用于刷新恢复。
    """

    __tablename__ = "chat_message"

    message_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversation.conversation_id", ondelete="CASCADE"),
        index=True,
    )
    # 关联的 task（可选：有些闲聊消息不触发 workflow，没有 task）
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("task.task_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # "user" = 用户说的话；"assistant" = 助手回复（非 workflow 的简短回复或错误提示）
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, default="")
    # 用户发送的图片附件元数据（[{name, storage_uri, content_type}]）
    images_json: Mapped[list] = mapped_column(JSON, default=list)
    # intent classifier 分类结果（debug/诊断用）
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 每个 conversation 内自增序号（前端恢复时按此排序）
    session_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
