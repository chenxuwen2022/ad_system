"""会话相关 REST API —— Conversation-centric 入口（async + AsyncSession）。

所有端点已切 AsyncSession（get_async_db from wellflow.app.database），
不再依赖 sync Session/anyio 线程池，支持几百人并发。

list_conversations 用 JOIN + 子查询一次拉齐 conversation 列表 + 统计
（msg_count / task_count / latest_user_preview / current_phase /
 current_saved_img_count / total_saved_img_count），彻底消除 N+1。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wellflow.app.database import get_async_db
from wellflow.app.api.utils import ok, StandardResponse, to_cn_iso
from wellflow.app.models.task_models import (
    Conversation, ChatMessage, Task, TaskImage, TaskEvent, TaskErrorLog, TaskPhase,
)
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
# 列表 —— 一次 JOIN 子查询拉齐全部统计，彻底消除 N+1
# ---------------------------------------------------------------------------


@router.get("", response_model=StandardResponse[ConversationListResponse], summary="列出会话（分页，按 updated_at 倒序）")
async def list_conversations(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_async_db),
):
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 100:
        page_size = 20

    # 总数
    total_result = await db.execute(select(func.count(Conversation.conversation_id)))
    total = total_result.scalar() or 0

    # ---- conversation 主查询（分页 + 按 updated_at 倒序） ----
    conv_stmt = (
        select(Conversation)
        .order_by(Conversation.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    conv_result = await db.execute(conv_stmt)
    items: list[Conversation] = list(conv_result.scalars().all())

    if not items:
        return ok(ConversationListResponse(items=[], total=total, page=page, page_size=page_size))

    conv_ids = [c.conversation_id for c in items]

    # ---- 批量统计（每个 conversation 的消息数 / task 数 / 总输出图数） ----
    # 子查询 + GROUP BY，一次查询拿到所有 conversation 的统计
    msg_count_stmt = (
        select(ChatMessage.conversation_id, func.count(ChatMessage.message_id).label("msg_count"))
        .where(ChatMessage.conversation_id.in_(conv_ids))
        .group_by(ChatMessage.conversation_id)
    )
    task_count_stmt = (
        select(Task.conversation_id, func.count(Task.task_id).label("task_count"))
        .where(Task.conversation_id.in_(conv_ids))
        .group_by(Task.conversation_id)
    )
    total_saved_img_stmt = (
        select(
            Task.conversation_id,
            func.count(TaskImage.image_id).label("total_saved_img_count"),
        )
        .join(Task, Task.task_id == TaskImage.task_id)
        .where(
            Task.conversation_id.in_(conv_ids),
            TaskImage.image_type == "output",
        )
        .group_by(Task.conversation_id)
    )

    # ---- latest_user_preview：每个 conversation 取 role=user 且 session_index 最大的那条 text ----
    # 用 row_number() over (partition by conversation_id order by session_index desc, message_id desc) 取 top 1
    rn = func.row_number().over(
        partition_by=ChatMessage.conversation_id,
        order_by=[ChatMessage.session_index.desc(), ChatMessage.message_id.desc()],
    ).label("rn")
    ranked_usr = (
        select(ChatMessage.conversation_id, ChatMessage.text, rn)
        .where(
            ChatMessage.conversation_id.in_(conv_ids),
            ChatMessage.role == "user",
        )
        .subquery()
    )
    latest_user_stmt = (
        select(ranked_usr.c.conversation_id, ranked_usr.c.text)
        .where(ranked_usr.c.rn == 1)
    )

    # ---- 当前活跃 task 及其 phase / 当前 task 的已保存输出图数 ----
    # conversation.current_task_id → Task.phase + count(TaskImage where image_type='output')
    current_task_stmt = (
        select(
            Conversation.conversation_id,
            Task.phase.label("current_phase"),
            func.coalesce(
                func.count(TaskImage.image_id)
                .filter(TaskImage.image_type == "output"),
                0,
            ).label("current_saved_img_count"),
        )
        .outerjoin(Task, Task.task_id == Conversation.current_task_id)
        .outerjoin(TaskImage, TaskImage.task_id == Conversation.current_task_id)
        .where(Conversation.conversation_id.in_(conv_ids))
        .group_by(Conversation.conversation_id, Task.phase)
    )

    # ---- 执行所有子查询 ----
    msg_count_rows = (await db.execute(msg_count_stmt)).all()
    task_count_rows = (await db.execute(task_count_stmt)).all()
    total_img_rows = (await db.execute(total_saved_img_stmt)).all()
    latest_user_rows = (await db.execute(latest_user_stmt)).all()
    current_task_rows = (await db.execute(current_task_stmt)).all()

    # ---- 组装成 dict 方便查 ----
    msg_count_map = {r[0]: r[1] for r in msg_count_rows}
    task_count_map = {r[0]: r[1] for r in task_count_rows}
    total_img_map = {r[0]: r[1] for r in total_img_rows}
    latest_user_map: dict[str, str] = {}
    for r in latest_user_rows:
        if r[1]:  # text 非空
            latest_user_map[r[0]] = str(r[1])[:80]
    current_task_map: dict[str, tuple[str, int]] = {}
    for r in current_task_rows:
        current_task_map[r[0]] = (r[1] or "", int(r[2]))

    # ---- 拼装响应 ----
    list_items: list[ConversationListItem] = []
    for c in items:
        current_phase, current_saved_img_count = current_task_map.get(
            c.conversation_id, ("", 0)
        )
        list_items.append(ConversationListItem(
            conversation_id=c.conversation_id,
            title=c.title,
            current_task_id=c.current_task_id,
            current_phase=current_phase,
            current_task_saved_image_count=current_saved_img_count,
            total_saved_image_count=total_img_map.get(c.conversation_id, 0),
            latest_message_preview=latest_user_map.get(c.conversation_id),
            message_count=msg_count_map.get(c.conversation_id, 0),
            task_count=task_count_map.get(c.conversation_id, 0),
            created_at=to_cn_iso(c.created_at),
            updated_at=to_cn_iso(c.updated_at),
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
async def get_conversation(conversation_id: str, db: AsyncSession = Depends(get_async_db)):
    # resolve：先按完整 PK 查，回退按 short_id
    c_result = await db.execute(
        select(Conversation).where(Conversation.conversation_id == conversation_id)
    )
    c = c_result.scalar_one_or_none()
    if not c:
        short_id = conversation_id.replace("-", "")[:12]
        c_result = await db.execute(
            select(Conversation).where(Conversation.conversation_id_short == short_id)
        )
        c = c_result.scalar_one_or_none()
    if not c:
        raise HTTPException(404, f"conversation {conversation_id} 不存在")

    # 消息列表（按 session_index 升序）
    msg_result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == c.conversation_id)
        .order_by(ChatMessage.session_index, ChatMessage.message_id)
    )
    msgs = list(msg_result.scalars().all())

    chat_out: list[ChatMessageOut] = []
    for m in msgs:
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
            created_at=to_cn_iso(m.created_at),
        ))

    # 关联 task 列表
    task_result = await db.execute(
        select(Task)
        .where(Task.conversation_id == c.conversation_id)
        .order_by(Task.created_at)
    )
    tasks = list(task_result.scalars().all())

    task_out: list[ConversationTaskRef] = []
    for t in tasks:
        req = t.request_json or {}
        task_out.append(ConversationTaskRef(
            task_id=t.task_id,
            phase=t.phase,
            description=req.get("description", "")[:80],
            has_interrupt=bool(t.interrupt_json),
            created_at=to_cn_iso(t.created_at),
            updated_at=to_cn_iso(t.updated_at),
        ))

    return ok(ConversationDetailResponse(
        conversation_id=c.conversation_id,
        title=c.title,
        current_task_id=c.current_task_id,
        messages=chat_out,
        tasks=task_out,
        created_at=to_cn_iso(c.created_at),
        updated_at=to_cn_iso(c.updated_at),
    ))


# ---------------------------------------------------------------------------
# Timeline —— 混合 chat_message + task_event 的完整时间线
# ---------------------------------------------------------------------------


@router.get(
    "/{conversation_id}/timeline",
    response_model=StandardResponse[dict[str, Any]],
    summary="查询会话时间线（对话消息 + 每个 task 的工作流事件）",
)
async def get_timeline(conversation_id: str, db: AsyncSession = Depends(get_async_db)):
    """返回 conversation 内所有 task 的工作流事件 + 全部 chat_message，
    按 created_at 合并成一条时间线。
    """
    from wellflow.app.models.task_models import TaskEvent

    # resolve conversation
    c_result = await db.execute(
        select(Conversation).where(Conversation.conversation_id == conversation_id)
    )
    c = c_result.scalar_one_or_none()
    if not c:
        short_id = conversation_id.replace("-", "")[:12]
        c_result = await db.execute(
            select(Conversation).where(Conversation.conversation_id_short == short_id)
        )
        c = c_result.scalar_one_or_none()
    if not c:
        raise HTTPException(404, f"conversation {conversation_id} 不存在")

    # 1) 拉 conversation 下所有 task
    tasks_result = await db.execute(
        select(Task)
        .where(Task.conversation_id == c.conversation_id)
        .order_by(Task.created_at)
    )
    tasks = list(tasks_result.scalars().all())
    task_ids = [t.task_id for t in tasks]

    # 2) 拉所有 task_event
    events: list[TaskEvent] = []
    if task_ids:
        events_result = await db.execute(
            select(TaskEvent)
            .where(TaskEvent.task_id.in_(task_ids))
            .order_by(TaskEvent.created_at)
        )
        events = list(events_result.scalars().all())

    # 3) 拉所有 chat_message
    chats_result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == c.conversation_id)
        .order_by(ChatMessage.session_index, ChatMessage.message_id)
    )
    chats = list(chats_result.scalars().all())

    # 4) 拼 timeline（统一结构，按时间升序）
    timeline: list[dict[str, Any]] = []

    for ch in chats:
        timeline.append({
            "kind": "chat",
            "task_id": ch.task_id,
            "role": ch.role,
            "text": ch.text or "",
            "intent": ch.intent,
            "session_index": ch.session_index,
            "created_at": to_cn_iso(ch.created_at),
        })

    for ev in events:
        timeline.append({
            "kind": "event",
            "event_id": ev.event_id,
            "task_id": ev.task_id,
            "event_type": ev.event_type,
            "phase": ev.phase,
            "payload": ev.payload_json or {},
            "cost_usd": ev.cost_usd,
            "created_at": to_cn_iso(ev.created_at),
        })

    # 5) 按 created_at 升序排（chat 没 created_at 的退到最后）
    def _sort_key(item: dict[str, Any]) -> tuple:
        ts = item.get("created_at") or "9999-99-99"
        idx = item.get("session_index") or 0
        return (ts, idx)

    timeline.sort(key=_sort_key)

    return ok({
        "conversation_id": c.conversation_id,
        "task_ids": task_ids,
        "timeline": timeline,
        "total": len(timeline),
    })


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------

# 正在执行中的 phase 集合 —— 进行中的任务禁止删除会话
_ACTIVE_TASK_PHASES = frozenset({
    TaskPhase.INPUT.value,
    TaskPhase.RESEARCH.value,
    TaskPhase.PLANNING.value,
    TaskPhase.DELIVERY.value,
})


@router.delete(
    "/{conversation_id}",
    response_model=StandardResponse[dict],
    summary="删除会话（级联删 task + checkpoint + chat_message；仅未入库会话才删磁盘图片）",
)
async def delete_conversation(conversation_id: str, db: AsyncSession = Depends(get_async_db)):
    """删除 conversation 及其全部关联数据。

    清理范围：
      - DB 记录：ChatMessage / TaskImage / TaskEvent / TaskErrorLog / Task / Conversation 全删
      - LangGraph checkpoint：对每个 task_id 调 adelete_thread
      - 磁盘文件：
          · 会话里**任何一个** task phase == "done"（用户在 C4 确认入库过）
            → 整个会话的磁盘图片**保留**（uploads/{task_id}/ 目录不动），
            避免让已入库的生图成品丢失。DB 记录照常清理。
          · 会话里**没有**任何 task 到 done（草稿 / 中途失败 / 只停在 HITL）
            → 删干净 uploads/{task_id}/ 下所有文件。

    防护：
      - 进行中的 task（phase ∈ input / research / planning / delivery）禁止删除
        —— 正在跑 graph 时删会话会导致 checkpoint 和 DB 状态失配。
    """
    import asyncio

    # ---- 1. resolve conversation ----
    c_result = await db.execute(
        select(Conversation).where(Conversation.conversation_id == conversation_id)
    )
    c = c_result.scalar_one_or_none()
    if not c:
        short_id = conversation_id.replace("-", "")[:12]
        c_result = await db.execute(
            select(Conversation).where(Conversation.conversation_id_short == short_id)
        )
        c = c_result.scalar_one_or_none()
    if not c:
        raise HTTPException(404, f"conversation {conversation_id} 不存在")

    # ---- 2. 查全部关联 task ----
    tasks_result = await db.execute(
        select(Task).where(Task.conversation_id == c.conversation_id)
    )
    tasks = list(tasks_result.scalars().all())
    task_ids = [t.task_id for t in tasks]

    # ---- 3. 活动任务防护（DB phase + checkpoint 双重校验）----
    # 后端重启后 DB phase 可能陈旧（停在 input 但 checkpoint 已 done），
    # 必须叠加 checkpoint rt_state 才能准确判定"真的在跑"；
    # 只有 DB phase ∈ active **且** checkpoint rt_state ∈ (running, stale) 才真正拦截
    from wellflow.app.runtime import get_graph
    from wellflow.app.graph_context import check_graph_runtime_state as _check_rt
    _graph = get_graph()

    _true_active: list[tuple[str, str]] = []
    for t in tasks:
        if t.phase not in _ACTIVE_TASK_PHASES:
            continue
        # DB 看起来 active → 再确认 checkpoint 是不是真在执行
        try:
            if _graph is None:
                raise RuntimeError("LangGraph 未初始化")
            config = {"configurable": {"thread_id": t.task_id}}
            snapshot = await _graph.aget_state(config)
            rt_state, _ = _check_rt(snapshot)
            if rt_state in ("running", "stale"):
                _true_active.append((t.task_id, t.phase))
            else:
                print(
                    f"[delete_conversation] 🛡️ task={t.task_id} "
                    f"DB phase={t.phase} 但 checkpoint rt_state={rt_state!r}，"
                    f"不计为活动 → 允许删除",
                    flush=True,
                )
        except Exception as _ckpt_err:
            # checkpoint 读不到 → 保守拦截（避免误删真在跑的任务）
            print(
                f"[delete_conversation] ⚠️ task={t.task_id} checkpoint 状态读不到，"
                f"保守按 DB phase 拦截: {_ckpt_err}",
                flush=True,
            )
            _true_active.append((t.task_id, t.phase))

    if _true_active:
        raise HTTPException(
            409,
            f"会话下有任务正在执行中（{_true_active}），请等待执行完成或人工确认后再删除",
        )

    # ---- 4. 判定磁盘图片是否保留 ----
    # 会话里任何一个 task 已经 phase=="done"（用户确认入库）→ 整个会话的磁盘图保留
    has_any_done = any(t.phase == TaskPhase.DONE.value for t in tasks)
    should_keep_images = has_any_done

    # ---- 5. DB 级联删除（所有 task 的子表 + 主表 + chat_message + conversation）----
    if task_ids:
        await db.execute(delete(TaskImage).where(TaskImage.task_id.in_(task_ids)))
        await db.execute(delete(TaskEvent).where(TaskEvent.task_id.in_(task_ids)))
        await db.execute(delete(TaskErrorLog).where(TaskErrorLog.task_id.in_(task_ids)))
        await db.execute(delete(Task).where(Task.task_id.in_(task_ids)))

    await db.execute(
        delete(ChatMessage).where(ChatMessage.conversation_id == c.conversation_id)
    )
    await db.execute(
        delete(Conversation).where(Conversation.conversation_id == c.conversation_id)
    )
    await db.commit()

    print(
        f"[delete_conversation] 🗑️ DB 已清 conversation={c.conversation_id} "
        f"tasks={len(task_ids)} should_keep_images={should_keep_images}",
        flush=True,
    )

    # ---- 6. 磁盘文件（DB commit 后再删，避免 rollback 后文件已没了）----
    if task_ids and not should_keep_images:
        from wellflow.app.utils.image_store import delete_task_files
        for tid in task_ids:
            await asyncio.to_thread(delete_task_files, tid)
        print(f"[delete_conversation] 🗑️ 已删 {len(task_ids)} 个任务目录", flush=True)

    # ---- 7. LangGraph checkpoint ----
    if task_ids:
        from wellflow.app.runtime import get_checkpointer
        cp = get_checkpointer()
        if cp is not None and hasattr(cp, "adelete_thread"):
            async def _del_thread(tid: str) -> None:
                try:
                    await cp.adelete_thread(tid)
                except Exception as e:
                    print(f"[delete_conversation] ⚠️ checkpoint 清理失败 task_id={tid}: {e}", flush=True)
            await asyncio.gather(*(_del_thread(tid) for tid in task_ids))
            print(f"[delete_conversation] ✅ checkpoint 已清 {len(task_ids)} 个任务", flush=True)

    return ok({
        "conversation_id": c.conversation_id,
        "deleted": True,
        "task_count": len(task_ids),
        "images_kept": should_keep_images,
    })
