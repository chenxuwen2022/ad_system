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

        # 最后一条用户消息预览：优先取 role=user 的 session_index 最大那条，
        # 确保预览稳定（assistant 的 resume_ack/error 等系统消息不应抢占 preview）
        preview: str | None = None
        latest_user = db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == c.conversation_id,
                ChatMessage.role == "user",
            )
            .order_by(desc(ChatMessage.session_index))
            .limit(1)
        ).scalar_one_or_none()
        if latest_user and latest_user.text:
            preview = latest_user.text[:80]

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
# Timeline —— 混合 chat_message + task_event 的完整时间线
# ---------------------------------------------------------------------------


@router.get(
    "/{conversation_id}/timeline",
    response_model=StandardResponse[dict[str, Any]],
    summary="查询会话时间线（对话消息 + 每个 task 的工作流事件）",
)
def get_timeline(conversation_id: str, db: Session = Depends(get_db)):
    """返回 conversation 内所有 task 的工作流事件 + 全部 chat_message，
    按 created_at 合并成一条时间线。

    前端可以完整恢复：用户消息 → Node1 报告 → C1 确认 → Node2 方案 → ... → 生图完成。
    无论用户做了多少次 backward 重跑，每一步 interrupt 的完整产物都会保留。

    timeline items 的 type 字段区分来源：
      - "chat"          → ChatMessage（对话文本）
      - "graph_interrupt_c1/c2/c3/c4" → TaskEvent interrupt 产物（report/schemes/prompts）
      - "phase_change"  → 节点完成 phase 变更
      - "workflow_done" → 整个生图完成
      - "workflow_error" → 节点报错
    """
    from wellflow.app.repositories.task_repo import TaskRepo, ChatMessageRepo

    conv_repo = ConversationRepo(db)
    c = conv_repo.resolve(conversation_id)
    if not c:
        raise HTTPException(404, f"conversation {conversation_id} 不存在")

    task_repo = TaskRepo(db)
    chat_repo = ChatMessageRepo(db)

    # 1. 拉 conversation 下所有 task
    tasks = task_repo.list_tasks_by_conversation(conversation_id)
    task_ids = [t.task_id for t in tasks]

    # 2. 拉所有 task_event
    events = task_repo.list_events_by_tasks(task_ids) if task_ids else []

    # 3. 拉所有 chat_message
    chats = chat_repo.list_by_conversation(conversation_id)

    # 4. 拼 timeline（统一结构，按时间升序）
    timeline: list[dict[str, Any]] = []

    for ch in chats:
        timeline.append({
            "kind": "chat",
            "task_id": ch.task_id,
            "role": ch.role,
            "text": ch.text or "",
            "intent": ch.intent,
            "session_index": ch.session_index,
            "created_at": ch.created_at.isoformat() if ch.created_at else None,
        })

    for ev in events:
        timeline.append({
            "kind": "event",
            "event_id": ev.event_id,
            "task_id": ev.task_id,
            "event_type": ev.event_type,  # graph_interrupt_c1 / phase_change / workflow_done ...
            "phase": ev.phase,
            "payload": ev.payload_json or {},
            "cost_usd": ev.cost_usd,
            "created_at": ev.created_at.isoformat() if ev.created_at else None,
        })

    # 5. 按 created_at 升序排（chat 没 created_at 的退到最后）
    def _sort_key(item: dict[str, Any]) -> tuple:
        ts = item.get("created_at") or "9999-99-99"
        idx = item.get("session_index") or 0
        return (ts, idx)

    timeline.sort(key=_sort_key)

    return ok({
        "conversation_id": conversation_id,
        "task_ids": task_ids,
        "timeline": timeline,
        "total": len(timeline),
    })


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
