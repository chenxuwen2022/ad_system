"""会话相关 REST API —— Conversation-centric 入口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, desc
from sqlalchemy.orm import Session

from wellflow.app.database import get_db
from wellflow.app.api.utils import ok, StandardResponse
from wellflow.app.models.task_models import Conversation, ChatMessage, Task
from wellflow.app.repositories.conversation_repo import ConversationRepo
from wellflow.app.schemas.conversation_schemas import (
    ConversationListResponse,
    ConversationListItem,
    ConversationDetailResponse,
    ChatMessageOut,
    ConversationTaskRef,
)

router = APIRouter(prefix="/conversations", tags=["会话"])


def _storage_uri_url(storage_uri: str) -> str:
    if not storage_uri:
        return ""
    if storage_uri.startswith("http"):
        return storage_uri
    return "/" + storage_uri.lstrip("/")


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------


@router.get("", response_model=StandardResponse[ConversationListResponse], summary="列出会话（分页，按 updated_at 倒序）")
def list_conversations(
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
):
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 100:
        page_size = 20

    count_stmt = select(func.count(Conversation.conversation_id))
    total = db.execute(count_stmt).scalar() or 0

    stmt = (
        select(Conversation)
        .order_by(desc(Conversation.updated_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = list(db.execute(stmt).scalars().all())

    list_items: list[ConversationListItem] = []
    for c in items:
        # 消息数
        msg_count = db.execute(
            select(func.count(ChatMessage.message_id)).where(
                ChatMessage.conversation_id == c.conversation_id
            )
        ).scalar() or 0

        # 关联 task 数
        task_count = db.execute(
            select(func.count(Task.task_id)).where(
                Task.conversation_id == c.conversation_id
            )
        ).scalar() or 0

        # 最后一条消息预览（取 session_index 最大的一条）
        preview: str | None = None
        latest = db.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == c.conversation_id)
            .order_by(desc(ChatMessage.session_index))
            .limit(1)
        ).scalar_one_or_none()
        if latest and latest.text:
            preview = latest.text[:80]

        list_items.append(ConversationListItem(
            conversation_id=c.conversation_id,
            title=c.title,
            current_task_id=c.current_task_id,
            latest_message_preview=preview,
            message_count=msg_count,
            task_count=task_count,
            created_at=c.created_at.isoformat(),
            updated_at=c.updated_at.isoformat(),
        ))

    return ok(ConversationListResponse(
        items=list_items,
        total=total,
        page=page,
        page_size=page_size,
    ))


# ---------------------------------------------------------------------------
# 详情
# ---------------------------------------------------------------------------


@router.get("/{conversation_id}", response_model=StandardResponse[ConversationDetailResponse], summary="查询会话详情（消息 + 关联 task 列表）")
def get_conversation(conversation_id: str, db: Session = Depends(get_db)):
    conv_repo = ConversationRepo(db)
    c = conv_repo.resolve(conversation_id)
    if not c:
        raise HTTPException(404, f"conversation {conversation_id} 不存在")

    # 消息列表（按 session_index 升序）—— 用 resolve 后的真实 PK，避免 short_id 查不到 FK
    msg_stmt = (
        select(ChatMessage)
        .where(ChatMessage.conversation_id == c.conversation_id)
        .order_by(ChatMessage.session_index, ChatMessage.message_id)
    )
    msgs = list(db.execute(msg_stmt).scalars().all())

    chat_out: list[ChatMessageOut] = []
    for m in msgs:
        # images_json 里如果有 storage_uri 字段，补 url
        imgs: list[dict[str, Any]] = []
        for img in (m.images_json or []):
            if isinstance(img, dict):
                imgs.append({**img, "url": _storage_uri_url(img.get("storage_uri", ""))})
        chat_out.append(ChatMessageOut(
            message_id=m.message_id,
            conversation_id=m.conversation_id,
            task_id=m.task_id,
            role=m.role,
            text=m.text or "",
            images=imgs,
            intent=m.intent,
            session_index=m.session_index,
            created_at=m.created_at.isoformat(),
        ))

    # 关联 task 列表
    task_stmt = (
        select(Task)
        .where(Task.conversation_id == c.conversation_id)
        .order_by(Task.created_at)
    )
    tasks = list(db.execute(task_stmt).scalars().all())

    task_out: list[ConversationTaskRef] = []
    for t in tasks:
        req = t.request_json or {}
        task_out.append(ConversationTaskRef(
            task_id=t.task_id,
            phase=t.phase,
            description=req.get("description", "")[:80],
            has_interrupt=bool(t.interrupt_json),
            created_at=t.created_at.isoformat(),
            updated_at=t.updated_at.isoformat(),
        ))

    return ok(ConversationDetailResponse(
        conversation_id=c.conversation_id,
        title=c.title,
        current_task_id=c.current_task_id,
        messages=chat_out,
        tasks=task_out,
        created_at=c.created_at.isoformat(),
        updated_at=c.updated_at.isoformat(),
    ))


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------


@router.delete("/{conversation_id}", response_model=StandardResponse[dict], summary="删除会话（级联删 chat_message，task 保持不变，conversation_id 置 NULL）")
def delete_conversation(conversation_id: str, db: Session = Depends(get_db)):
    conv_repo = ConversationRepo(db)
    c = conv_repo.resolve(conversation_id)
    if not c:
        raise HTTPException(404, f"conversation {conversation_id} 不存在")

    # 级联删 chat_message（FK ondelete=CASCADE 自动处理，但显式写一遍更安全）
    db.query(ChatMessage).where(ChatMessage.conversation_id == c.conversation_id).delete(
        synchronize_session=False
    )
    # Task.conversation_id 置 NULL（ondelete=SET NULL 已处理，保险起见显式）
    db.query(Task).where(Task.conversation_id == c.conversation_id).update(
        {Task.conversation_id: None}, synchronize_session=False
    )
    db.delete(c)
    db.commit()

    return ok({"conversation_id": c.conversation_id, "deleted": True})
