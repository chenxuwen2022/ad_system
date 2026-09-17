"""会话 & 消息数据访问层。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, func, desc
from sqlalchemy.orm import Session

from wellflow.app.models.task_models import Conversation, ChatMessage


class ConversationRepo:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get(self, conversation_id: str) -> Conversation | None:
        return self.db.get(Conversation, conversation_id)

    def create(
        self,
        conversation_id: str | None = None,
        title: str = "新对话",
        current_task_id: str | None = None,
    ) -> Conversation:
        cid = conversation_id or uuid.uuid4().hex[:12]
        obj = Conversation(
            conversation_id=cid,
            title=title[:128] if title else "新对话",
            current_task_id=current_task_id,
        )
        self.db.add(obj)
        self.db.commit()
        return obj

    def update_title(self, conversation_id: str, title: str) -> None:
        obj = self.get(conversation_id)
        if obj:
            obj.title = title[:128]
            obj.updated_at = datetime.utcnow()
            self.db.commit()

    def update_current_task(self, conversation_id: str, task_id: str | None) -> None:
        obj = self.get(conversation_id)
        if obj:
            obj.current_task_id = task_id
            obj.updated_at = datetime.utcnow()
            self.db.commit()

    def touch(self, conversation_id: str) -> None:
        """更新 updated_at（用于列表排序）。"""
        obj = self.get(conversation_id)
        if obj:
            obj.updated_at = datetime.utcnow()
            self.db.commit()

    def delete(self, conversation_id: str) -> bool:
        obj = self.get(conversation_id)
        if not obj:
            return False
        self.db.delete(obj)
        self.db.commit()
        return True

    # ------------------------------------------------------------------
    # 列表查询
    # ------------------------------------------------------------------

    def list_conversations(
        self,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Conversation], int]:
        """返回 (items, total_count)。按 updated_at 倒序。"""
        stmt = select(Conversation)
        count_stmt = select(func.count(Conversation.conversation_id))
        total = self.db.execute(count_stmt).scalar() or 0
        stmt = stmt.order_by(desc(Conversation.updated_at))
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        items = list(self.db.execute(stmt).scalars().all())
        return items, total


class ChatMessageRepo:
    def __init__(self, db: Session):
        self.db = db

    def list_by_conversation(self, conversation_id: str) -> list[ChatMessage]:
        """按 session_index 升序返回该 conversation 的全部消息。"""
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.session_index, ChatMessage.message_id)
        )
        return list(self.db.execute(stmt).scalars().all())

    def next_session_index(self, conversation_id: str) -> int:
        """取当前最大 session_index + 1（同一 conversation 内自增）。"""
        stmt = (
            select(func.max(ChatMessage.session_index))
            .where(ChatMessage.conversation_id == conversation_id)
        )
        result = self.db.execute(stmt).scalar()
        return (result or 0) + 1

    def create(
        self,
        conversation_id: str,
        role: str,
        text: str = "",
        images_json: list[dict[str, Any]] | None = None,
        intent: str | None = None,
        task_id: str | None = None,
        session_index: int | None = None,
    ) -> ChatMessage:
        if session_index is None:
            session_index = self.next_session_index(conversation_id)
        obj = ChatMessage(
            conversation_id=conversation_id,
            role=role,
            text=text or "",
            images_json=images_json or [],
            intent=intent,
            task_id=task_id,
            session_index=session_index,
        )
        self.db.add(obj)
        self.db.commit()
        return obj

    def count_for_conversation(self, conversation_id: str) -> int:
        stmt = (
            select(func.count(ChatMessage.message_id))
            .where(ChatMessage.conversation_id == conversation_id)
        )
        return self.db.execute(stmt).scalar() or 0
