"""Conversation / ChatMessage API 的 Pydantic schema。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ConversationListItem(BaseModel):
    conversation_id: str
    title: str = ""
    current_task_id: str | None = None
    # 最后一条消息预览（可选，用于前端显示副标题）
    latest_message_preview: str | None = None
    message_count: int = 0
    task_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class ConversationListResponse(BaseModel):
    items: list[ConversationListItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


class ChatMessageOut(BaseModel):
    message_id: int
    conversation_id: str
    task_id: str | None = None
    role: str
    text: str = ""
    images: list[dict[str, Any]] = Field(default_factory=list)
    intent: str | None = None
    session_index: int = 0
    created_at: str = ""


class ConversationTaskRef(BaseModel):
    task_id: str
    phase: str = ""
    description: str = ""
    has_interrupt: bool = False
    created_at: str = ""
    updated_at: str = ""


class ConversationDetailResponse(BaseModel):
    conversation_id: str
    title: str = ""
    current_task_id: str | None = None
    messages: list[ChatMessageOut] = Field(default_factory=list)
    # 关联的所有 task（用于前端恢复 workflow 节点产物）
    tasks: list[ConversationTaskRef] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
