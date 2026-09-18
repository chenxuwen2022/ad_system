"""POST /api/chat —— 对话入口。把用户自然语言路由到正确的 LangGraph 节点。

流程：
  1. 意图分类（关键词 → LLM）
  2. 跳步检测
  3. dispatch 到 async generator handler

所有 import 使用 wellflow.app.xxx（此项目的包前缀）。
"""

from __future__ import annotations

import asyncio
import json as json_mod
import re
import uuid
from typing import Any, AsyncGenerator

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from sqlalchemy.orm import Session

from wellflow.app.database import get_db, session_scope
from wellflow.app.event_bus import (
    publish, drain_and_subscribe, cleanup as _eb_cleanup,
    mark_running, mark_done, is_running,
)
from wellflow.app.repositories.task_repo import TaskRepo
from wellflow.app.repositories.conversation_repo import (
    ConversationRepo, ChatMessageRepo,
)
from wellflow.app.llm.intent_classifier import (
    classify,
    build_redo_options,
    compute_completed_mask,
    detect_skip,
    STEP_NAMES,
)

router = APIRouter(prefix="/chat", tags=["对话"])


def _short_uuid() -> str:
    return uuid.uuid4().hex[:12]


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json_mod.dumps(data, ensure_ascii=False)}\n\n"


def _get_graph():
    from wellflow.app.runtime import get_graph as _gg
    g = _gg()
    if g is None:
        raise RuntimeError("LangGraph 未初始化")
    return g


def _langgraph_config(task_id: str):
    from langchain_core.runnables import RunnableConfig
    return RunnableConfig(configurable={"thread_id": task_id})


# ---------------------------------------------------------------------------
# 图片分类规则：images → (product_images, model_images)
# ---------------------------------------------------------------------------

# 第二层关键词：命中则覆盖上下文路由
_PRODUCT_KEYWORDS = [
    r"商品图", r"产品图", r"商品照片", r"换商品", r"换产品",
    r"redo.*node1", r"重做.*商品", r"重新.*分析.*商品", r"重新分析商品",
    r"换.*产品", r"产品.*换",
]
_MODEL_KEYWORDS = [
    r"模特图", r"模特照片", r"参考图", r"模特参考",
    r"人物", r"模特", r"穿搭", r"真人",
]


def _match_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text) for p in patterns)


def classify_images(
    images: list[UploadFile],
    *,
    has_task: bool,
    current_node: str | None,
    message: str = "",
) -> tuple[list[UploadFile], list[UploadFile]]:
    """把统一的 images 列表分流为 (product_images, model_images)。

    三层优先级：
      1. message 关键词覆盖（命中 PRODUCT → 全部 product；命中 MODEL → 全部 model）
      2. 上下文路由（无 task → product；有 task + c1/c2/c3 → model）
      3. 兜底 → model_images

    不做 VLM 视觉分类（v1），保持零延迟。
    """
    if not images:
        return [], []

    msg = message.strip()

    # 第二层：关键词覆盖
    if _match_any(msg, _PRODUCT_KEYWORDS):
        print(f"[classify_images] 关键词覆盖 → product ({len(images)} 张)", flush=True)
        return list(images), []
    if _match_any(msg, _MODEL_KEYWORDS):
        print(f"[classify_images] 关键词覆盖 → model ({len(images)} 张)", flush=True)
        return [], list(images)

    # 第一层：上下文路由
    if not has_task:
        print(f"[classify_images] 无任务 → product ({len(images)} 张)", flush=True)
        return list(images), []
    if current_node in ("c1", "c2"):
        # C1/C2 阶段用户上传的图默认为商品参考图（可追加替换商品图）
        print(f"[classify_images] 上下文 c={current_node} → product ({len(images)} 张)", flush=True)
        return list(images), []
    if current_node in ("c3", "c4"):
        print(f"[classify_images] 上下文 c={current_node} → model ({len(images)} 张)", flush=True)
        return [], list(images)

    # 兜底
    print(f"[classify_images] 兜底 → product ({len(images)} 张)", flush=True)
    return list(images), []


async def _aget_snapshot(task_id: str) -> tuple[Any, dict[str, Any] | None]:
    """返回 (snapshot, snapshot.values)。两者都拿不到时返回 (None, None)。

    必须同时返回 snapshot —— graph_context.resolve_current_node 要读 snapshot.next。
    """
    from wellflow.app.runtime import get_graph as _gg
    g = _gg()
    if g is None:
        return None, None
    try:
        snapshot = await g.aget_state(_langgraph_config(task_id))
    except Exception as exc:
        print(f"[chat] aget_state 失败 task={task_id}: {exc}", flush=True)
        return None, None
    values = snapshot.values if snapshot and hasattr(snapshot, "values") else None
    return snapshot, values


def _extract_interrupt_value(chunk: Any) -> dict[str, Any] | None:
    if not isinstance(chunk, tuple) or len(chunk) != 2:
        return None
    mode, data = chunk
    if mode != "updates" or not isinstance(data, dict):
        return None
    interrupt_tuple = data.get("__interrupt__")
    if not interrupt_tuple or not isinstance(interrupt_tuple, tuple):
        return None
    interrupt_obj = interrupt_tuple[0]
    return getattr(interrupt_obj, "value", None)


async def _handle_graph_chunk(task_id: str, chunk: Any) -> None:
    from wellflow.app.event_bus import publish as _eb
    from wellflow.app.graph_persist import persist_interrupt, persist_phase
    interrupt_value = _extract_interrupt_value(chunk)
    if interrupt_value:
        print(f"[chat] ✅ interrupt! node={interrupt_value.get('node')}", flush=True)
        phase = f"{interrupt_value.get('node')}_confirm"
        _eb(task_id, "phase", {"phase": phase})
        _eb(task_id, "interrupt", {**interrupt_value, "_phase": phase})
        # 按流顺序落库：fire-and-forget 会让更早的 persist_phase 晚提交，覆盖 interrupt 的 phase
        await asyncio.to_thread(persist_interrupt, task_id, interrupt_value, phase)
        return

    if isinstance(chunk, tuple) and chunk[0] == "updates":
        data = chunk[1]
        if not isinstance(data, dict):
            return
        for node_name, node_out in data.items():
            if node_name == "__interrupt__" or not isinstance(node_out, dict):
                continue
            phase = node_out.get("phase")
            if not phase:
                continue
            _eb(task_id, "phase", {"phase": phase})
            await asyncio.to_thread(persist_phase, task_id, phase, node_name)
            if phase == "done":
                n3_raw = node_out.get("node3") or {}
                if n3_raw.get("reference_images"):
                    from wellflow.app.utils.image_store import paths_to_data_uris
                    n3_raw = {**n3_raw, "reference_images": paths_to_data_uris(n3_raw["reference_images"])}
                _eb(task_id, "done", {
                    "phase": "done",
                    "node1": node_out.get("node1"),
                    "node2": node_out.get("node2"),
                    "node3": n3_raw,
                })


async def _start_graph(task_id: str, graph, config, *, initial_state=None, command=None):
    import traceback as _tb
    try:
        mark_running(task_id)
        if command is not None:
            stream_iter = graph.astream(command, config=config, stream_mode=["updates"])
        elif initial_state is not None:
            stream_iter = graph.astream(initial_state, config=config, stream_mode=["updates"])
        else:
            return
        async for chunk in stream_iter:
            await _handle_graph_chunk(task_id, chunk)
    except Exception as exc:
        print(f"[chat] ❌ graph error task={task_id}: {exc}", flush=True)
        _tb.print_exc()
        try:
            from wellflow.app.event_bus import publish as _eb
            _eb(task_id, "error", {"phase": "failed", "message": str(exc)})
        except Exception:
            pass
    finally:
        mark_done(task_id)
        _eb_cleanup(task_id)


def _sync_db_after_jump(task_id: str, target_node_clean: str, clean_nodes: list[str]):
    try:
        with session_scope() as db:
            repo = TaskRepo(db)
            repo.save_interrupt(task_id, None)
            repo.update_phase(task_id, f"{target_node_clean}_confirm")
            repo.add_event(task_id, "graph_jumped",
                payload_json={"to": target_node_clean, "cleaned": clean_nodes})
        print(f"[chat] ✅ DB 同步完成 task={task_id} → {target_node_clean}", flush=True)
    except Exception as exc:
        print(f"[chat] ⚠️ DB 同步失败 task={task_id}: {exc}", flush=True)


async def _stream_queue(task_id: str, q) -> AsyncGenerator[str, None]:
    while True:
        try:
            event = await asyncio.wait_for(q.get(), timeout=25)
            etype = event.get("type")
            edata = event.get("data", {})
            # print(f"[chat:stream] task={task_id} ← {etype}", flush=True)

            yield _handle_sse_event(etype, edata)

            if etype in ("done", "error"):
                break
            if etype == "phase" and edata.get("phase") == "failed":
                break
            if etype == "interrupt":
                break
        except asyncio.TimeoutError:
            yield ": heartbeat\n\n"


def _handle_sse_event(event_type: str, event_data: dict[str, Any]) -> str:
    if event_type == "phase":
        return _sse("phase", {"phase": event_data.get("phase")})
    if event_type == "interrupt":
        return _sse("interrupt", event_data)
    if event_type == "thinking_chunk":
        return _sse("thinking_chunk", event_data)
    if event_type == "report_chunk":
        return _sse("report_chunk", event_data)
    if event_type == "report_chunk_done":
        return _sse("report_chunk_done", event_data)
    if event_type == "scheme_chunk":
        return _sse("scheme_chunk", event_data)
    if event_type == "scheme_chunk_done":
        return _sse("scheme_chunk_done", event_data)
    if event_type == "prompt_chunk":
        return _sse("prompt_chunk", event_data)
    if event_type == "prompt_chunk_done":
        return _sse("prompt_chunk_done", event_data)
    if event_type == "node3_image_done":
        return _sse("node3_image_done", event_data)
    if event_type == "done":
        return _sse("done", event_data)
    if event_type == "error":
        return _sse("error", event_data)
    return ""


# ===========================================================================
# POST /api/chat
# ===========================================================================


@router.post("", summary="对话入口（意图路由 + SSE）")
async def chat(
    message: str = Form(default=""),
    task_id: str | None = Form(default=None),
    # 新增：conversation 关联（可选，首次可不传；后端会从 task.conversation_id 反查或自动新建）
    conversation_id: str | None = Form(default=None),
    platform: str = Form(default="taobao"),
    image_type: str = Form(default="ad"),
    marketing_goal: str = Form(default="acquisition"),
    # c2 界面 checkbox 选中的方案索引（逗号分隔，如 "0,2"），前端输入框发消息时带上
    selected_scheme_indices: str | None = Form(default=None),
    # 用户在 redo radio 弹层里选的目标节点（node1/node2/node3/node4），
    # 有此值时跳过意图分类直接走 _handle_backward
    selected_redo_target: str | None = Form(default=None),
    images: list[UploadFile] = File(default_factory=list),
    db: Session = Depends(get_db),
):
    # ────────────────────────────────────── UTF-8 容错 ──────────────────────────────────────
    # macOS curl / 部分客户端用 latin-1 解 Form（Content-Type 无 charset），
    # 中文字节会被当成 latin-1 字符 → "重来第一步" 变成乱码。
    # 这里做一次纠正：如果 str 里含非 ASCII 且 encode('latin-1').decode('utf-8') 能成功，
    # 说明它是 UTF-8 bytes 被 latin-1 解过，就纠正回来。
    def _fix_utf8(s: str) -> str:
        try:
            if s and any(ord(c) > 127 for c in s):
                fixed = s.encode("latin-1").decode("utf-8")
                print(f"[chat] ✅ 纠正 latin-1→utf-8: {s!r} → {fixed!r}", flush=True)
                return fixed
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return s

    message = _fix_utf8(message).strip()

    has_task = bool(task_id)
    t_id: str | None = None
    current_node: str | None = None
    completed_mask = [False] * 6
    existing_report = ""
    existing_schemes: list = []
    existing_model_images: list[str] = []

    # conversation 关联解析：
    # 1) 前端显式传了 conversation_id → 用它
    # 2) 有 task → 从 task.conversation_id 反查
    # 3) 都没有 → event_generator 里 start_task 时自动新建
    conv_repo = ConversationRepo(db)
    msg_repo = ChatMessageRepo(db)
    resolved_conv_id: str | None = conversation_id

    if has_task:
        repo = TaskRepo(db)
        t_id = str(task_id)
        task = repo.get(t_id)
        if not task:
            raise HTTPException(404, f"task {t_id} 不存在")
        # 反查 conversation_id（前端没传但 task 已有）
        if not resolved_conv_id and task.conversation_id:
            resolved_conv_id = task.conversation_id
            print(f"[chat] 📎 从 task.conversation_id 反查 conversation={resolved_conv_id}", flush=True)

        # ---------- 🔑 current_node 多源校验（核心修复）----------
        # 以 LangGraph checkpoint snapshot 为主（唯一真相源），DB 的 interrupt_json/phase 为辅。
        # snapshot 包含 graph 运行时的完整状态；DB 层可能因 _clear_interrupt / 异常中断而丢失。
        from wellflow.app.graph_context import resolve_current_node
        snapshot, graph_state = await _aget_snapshot(t_id)
        ctx = resolve_current_node(
            snapshot=snapshot,
            state=graph_state,
            db_interrupt_json=task.interrupt_json,
            db_phase=task.phase,
        )
        current_node = ctx.current_node

        # ---------- 从 state 取产物 ----------
        if graph_state:
            completed_mask = compute_completed_mask(graph_state, current_node)
            existing_report = str(graph_state.get("node1", {}).get("product_insight", "") or "")
            # c2 的可选项是 schemes（方案），不是 prompts —— 历史上误读 node2.generate_prompts
            # （该字段属于 node3），导致 existing_schemes 永远为空，"默认全选"退化成"全不选"
            existing_schemes = list(graph_state.get("node2", {}).get("schemes", []) or [])
            existing_model_images = list(graph_state.get("node3", {}).get("model_images", []) or [])
        print(f"[chat] 上下文: task={t_id} node={current_node} completed={completed_mask}"
              f" model_images_in_state={len(existing_model_images)}"
              f" conversation={resolved_conv_id}", flush=True)

    # 🔑 统一 images → 后端判断分流
    product_images, model_images = classify_images(
        images, has_task=has_task, current_node=current_node, message=message,
    )

    # ------------------------------------------------------------------
    # 🎯 selected_redo_target：用户在 redo radio 弹层里选了目标节点 →
    # 跳过意图分类，直接组装 backward_to_nodeX intent 走 dispatch。
    # 校验 node 合法性（守卫），不合法直接拦截。
    # ------------------------------------------------------------------
    redo_target_from_radio: str | None = None
    if selected_redo_target and has_task:
        allowed = build_redo_options(current_node)
        allowed_values = {opt["value"] for opt in allowed}
        if selected_redo_target in allowed_values:
            redo_target_from_radio = selected_redo_target
            intent = f"backward_to_{selected_redo_target}"
            intent_result = {"intent": intent, "reasoning": f"redo radio 回传→{intent}",
                             "selected_indices": None, "blocked_step": None, "parsed_content": None}
            print(f"[chat] 🎯 selected_redo_target={selected_redo_target} → 跳过分类，intent={intent}", flush=True)
        else:
            print(f"[chat] 🛡️ selected_redo_target={selected_redo_target} 不在允许列表 {allowed_values}，拦截", flush=True)
            async def _bad_target_sse():
                yield _sse("message", {"text": f"当前阶段不允许回退到 {selected_redo_target}"})
                yield _sse("done", {"phase": "blocked"})
            return StreamingResponse(_bad_target_sse(), media_type="text/event-stream")
    else:
        intent_result = await classify(
            message,
            has_task=has_task,
            current_node=current_node,
            completed_mask=completed_mask,
            has_images=len(images) > 0,
            product_description=message[:500],
        )
        intent = intent_result.get("intent", "chat_outside")
        print(f"[chat] 意图={intent} reason={intent_result.get('reasoning', '')[:60]}", flush=True)

    # ------------------------------------------------------------------
    # 🛡️ 意图守卫拦截：返回 blocked=True → 直接 SSE 友好消息，绝对不启动 graph。
    # 防止不合法的 backward_to_nodeX 穿透到 graph 重跑大模型。
    # ------------------------------------------------------------------
    if intent_result.get("blocked"):
        blocked_reason = intent_result.get("blocked_reason", "当前操作不允许")
        print(f"[chat] 🛡️ 意图守卫拦截: {blocked_reason}", flush=True)
        async def _blocked_sse():
            yield _sse("message", {"text": blocked_reason})
            yield _sse("done", {"phase": "blocked"})
        return StreamingResponse(_blocked_sse(), media_type="text/event-stream")

    # ------------------------------------------------------------------
    # 🛡️ start_task 守卫：已有任务时绝不允许开新任务跑 Node1。
    # LLM 兜底分类可能把 "继续" 这类短确认误判成 start_task（尤其 current_node
    # 丢失时），若直接 _handle_start_task 会抛弃当前任务从头跑 → 强制降级为
    # confirm_current 走 resume（current_node 缺失时 _handle_resume 有兜底提示）。
    # ------------------------------------------------------------------
    if intent == "start_task" and has_task:
        print(f"[chat] 🛡️ 已有 task={t_id}，start_task 降级 → confirm_current", flush=True)
        intent = "confirm_current"
        intent_result["intent"] = "confirm_current"
        intent_result["reasoning"] = f"守卫: has_task=True 时拦截 start_task（原 reasoning: {intent_result.get('reasoning', '')}）"

    # ------------------------------------------------------------------
    # Conversation 关联 + user chat_message 持久化（在 event_generator 之外同步做）
    # 新建 conversation（首次）或复用已解析的 resolved_conv_id
    # ------------------------------------------------------------------
    # 收集用户图片附件元数据（不重复读文件）
    user_images_meta: list[dict[str, str]] = []
    for img in images:
        user_images_meta.append({
            "name": img.filename or "image",
            "content_type": img.content_type or "",
        })

    # start_task → 新建 conversation（优先用前端传的，否则后端生成）；否则用已解析的
    conv_id_for_this_turn: str | None = resolved_conv_id
    if intent == "start_task":
        # 优先复用前端传的 conversation_id（前端用 createId() 生成，全局唯一）
        if not conv_id_for_this_turn:
            conv_id_for_this_turn = _short_uuid()
            _title = message.strip()[:30] or "新对话"
            conv_repo.create(conversation_id=conv_id_for_this_turn, title=_title)
            print(f"[chat] ✨ 新建 conversation={conv_id_for_this_turn} title={_title}", flush=True)
        else:
            # 前端传了但还没建（首次 start_task，前端 generate 的 id 后端还没记录）
            # 用 resolve() 兼容 short_id / 完整 UUID
            existing = conv_repo.resolve(conv_id_for_this_turn)
            if not existing:
                _title = message.strip()[:30] or "新对话"
                conv_repo.create(conversation_id=conv_id_for_this_turn, title=_title)
                print(f"[chat] ✨ 复用前端 conversation_id={conv_id_for_this_turn}", flush=True)

    # 归一化：确保 conv_id_for_this_turn 是完整主键（short_id / UUID 都能解析）
    # 下游 persist / touch / update_current_task 需要真实 PK
    if conv_id_for_this_turn:
        _resolved_obj = conv_repo.resolve(conv_id_for_this_turn)
        if _resolved_obj and _resolved_obj.conversation_id != conv_id_for_this_turn:
            print(f"[chat] 🔄 归一化 conversation_id {conv_id_for_this_turn} → {_resolved_obj.conversation_id}", flush=True)
            conv_id_for_this_turn = _resolved_obj.conversation_id

    # 写 user chat_message（无论什么 intent 都写，保留完整对话历史）
    if conv_id_for_this_turn:
        try:
            msg_repo.create(
                conversation_id=conv_id_for_this_turn,
                role="user",
                text=message,
                images_json=user_images_meta,
                intent=intent,
                task_id=t_id,  # start_task 时 t_id 还没，是 None，后面会关联
            )
            conv_repo.touch(conv_id_for_this_turn)
            print(f"[chat] 💬 user message 已持久化 conv={conv_id_for_this_turn} intent={intent}", flush=True)
        except Exception as exc:
            print(f"[chat] ⚠️ user message 持久化失败（不阻断主流程）: {exc}", flush=True)

    async def _persist_assistant_msg(text: str, task_id_: str | None = None) -> None:
        """fire-and-forget 写一条 assistant chat_message。

        注意：这里的 db session 不能跨 event_generator 的 yield 持有，
        所以用 session_scope 开新的上下文。
        """
        if not conv_id_for_this_turn or not text:
            return
        try:
            def _sync_write():
                from wellflow.app.repositories.conversation_repo import (
                    ChatMessageRepo,
                    ConversationRepo,
                )
                with session_scope() as sdb:
                    ChatMessageRepo(sdb).create(
                        conversation_id=conv_id_for_this_turn,
                        role="assistant",
                        text=text,
                        task_id=task_id_,
                    )
                    ConversationRepo(sdb).touch(conv_id_for_this_turn)
            await asyncio.to_thread(_sync_write)
        except Exception as exc:
            print(f"[chat] ⚠️ assistant message 持久化失败: {exc}", flush=True)

    async def _persist_sse_text(raw_sse: str, *, known_task_id: str | None = None) -> None:
        """从 SSE chunk 里抽取 assistant 文本并持久化（幂等：有文本才写）。

        识别的事件类型：
          - event: message       → data.text
          - event: resume_ack    → data.message
          - event: error         → data.message / data.error
        """
        try:
            lines = raw_sse.splitlines()
            ev_type = next(
                (ln[7:].strip() for ln in lines if ln.startswith("event:")),
                "",
            )
            data_line = next(
                (ln[5:].strip() for ln in lines if ln.startswith("data:")),
                "",
            )
            if not ev_type or not data_line:
                return
            data = json_mod.loads(data_line)
        except Exception:
            return
        text = ""
        if ev_type == "message":
            text = str(data.get("text") or "").strip()
        elif ev_type == "resume_ack":
            text = str(data.get("message") or "").strip()
        elif ev_type == "error":
            text = str(data.get("message") or data.get("error") or "").strip()
        if text:
            await _persist_assistant_msg(text, known_task_id)

    async def _stream_with_persist(
        generator: AsyncGenerator[str, None],
        *,
        task_id_getter,
    ) -> AsyncGenerator[str, None]:
        """包一层：每 yield 一个 SSE chunk 前，自动持久化有文本的 assistant 回复。

        task_id_getter 是一个 callable，每次被调用时返回当前最新的 task_id（或 None），
        解决 start_task 时 task_id 在 handler 内部才生成的时序问题。
        """
        async for chunk in generator:
            await _persist_sse_text(chunk, known_task_id=task_id_getter())
            yield chunk

    async def event_generator() -> AsyncGenerator[str, None]:
        # early-return 分支：手动 persist 每个有文本的 yield
        if intent == "chat_outside":
            chunk = _sse("message", {"text": "暂不支持与生图无关的对话。"})
            await _persist_sse_text(chunk, known_task_id=t_id)
            yield chunk
            yield _sse("done", {"phase": "done"})
            return

        blocked = intent_result.get("blocked_step")
        if intent == "skip_forward" or blocked:
            # 优先把 LLM 原始 reasoning 透传给前端，比硬编码更准确
            reasoning = str(intent_result.get("reasoning") or "").strip()
            msg = ""
            if isinstance(blocked, dict):
                idx = blocked.get("index")
                name = blocked.get("name")
                if isinstance(idx, int) and name:
                    msg = f"不支持跳过第 {idx} 步（{name}），请先完成它。"
                else:
                    msg = str(blocked.get("message", "不支持跳过工作流步骤。"))
            if not msg and reasoning:
                msg = reasoning
            elif not msg:
                msg = "不支持跳过工作流中的步骤。"
            chunk = _sse("message", {"text": msg})
            await _persist_sse_text(chunk, known_task_id=t_id)
            yield chunk
            yield _sse("done", {"phase": "done"})
            return

        # ------------------------------------------------------------------
        # 运行状态守卫：同一个 task 并发请求 → 直接返回，防止 graph 冲突
        # ------------------------------------------------------------------
        if has_task and t_id and intent != "start_task" and is_running(t_id):
            print(f"[chat] 🛡️ task={t_id} 正在执行中，拒绝 intent={intent}", flush=True)
            chunk = _sse("message", {"text": "任务正在执行中，请等待当前操作完成后再试。"})
            await _persist_sse_text(chunk, known_task_id=t_id)
            yield chunk
            yield _sse("done", {"phase": "done"})
            return

        try:
            graph = _get_graph()
        except Exception as exc:
            yield _sse("error", {"phase": "failed", "message": str(exc)})
            yield _sse("done", {"phase": "failed"})
            return

        try:
            # _pipe 包装：让 _stream_with_persist 每次都能拿到最新 t_id（通过 getter），
            # 同时 start_task 时从 task_created 事件里抓到 handler 内部生成的 task_id
            async def _pipe(generator):
                nonlocal t_id
                async for ev in _stream_with_persist(generator, task_id_getter=lambda: t_id):
                    if not t_id:
                        try:
                            lines = ev.splitlines()
                            ev_type = next(
                                (ln[7:].strip() for ln in lines if ln.startswith("event:")),
                                "",
                            )
                            data_line = next(
                                (ln[5:].strip() for ln in lines if ln.startswith("data:")),
                                "",
                            )
                            if ev_type == "task_created" and data_line:
                                _data = json_mod.loads(data_line)
                                _tid = str(_data.get("task_id") or "")
                                if _tid:
                                    t_id = _tid
                        except Exception:
                            pass
                    yield ev

            # ------------------------------------------------------------------
            # 🛡️ graph runtime 守卫（独立前置检查，不在 if-elif-else 链里）：
            #   paused  → 放行到下面的 dispatch
            #   running → 拦截（graph 真在执行）
            #   stale   → 尝试 astream(None) 续跑；恢复失败则拦截
            #   done    → 放行（让 redo/backward_to_nodeX 走 _handle_backward 路径 B）
            # ------------------------------------------------------------------
            _NEEDS_RUNTIME_CHECK = (
                "confirm_current", "confirm_generation", "redo",
                "backward_to_node1", "backward_to_node2",
                "backward_to_node3", "backward_to_node4",
            )
            if has_task and intent in _NEEDS_RUNTIME_CHECK:
                from wellflow.app.graph_context import check_graph_runtime_state
                _rt_state, _rt_age = check_graph_runtime_state(snapshot)
                if _rt_state == "done":
                    print(f"[chat] ✅ graph已END(done)，放行 intent={intent}", flush=True)
                elif _rt_state == "running":
                    print(f"[chat] 🛡️ graph 正在执行（checkpoint 年龄 {_rt_age:.0f}s），拦截 intent={intent}",
                          flush=True)
                    yield _sse("message", {
                        "text": "任务正在执行中，暂不支持当前操作，请稍候再试。",
                    })
                    yield _sse("done", {"phase": "processing"})
                    return
                elif _rt_state == "stale":
                    print(f"[chat] ⚠️ graph checkpoint 过旧（{_rt_age:.0f}s），"
                          f"可能已挂，尝试 astream(None) 恢复 intent={intent}",
                          flush=True)
                    try:
                        graph = _get_graph()
                        if graph is None:
                            raise RuntimeError("LangGraph 未初始化")
                        config = _langgraph_config(t_id)
                        async for _chunk in graph.astream(None, config, stream_mode="updates"):
                            pass
                        print(f"[chat] ✅ astream(None) 续跑完成", flush=True)
                        _, _new_snap = await _aget_snapshot(t_id)
                        if _new_snap and hasattr(_new_snap, "next"):
                            from wellflow.app.graph_context import is_graph_paused
                            if is_graph_paused(_new_snap):
                                yield _sse("message", {
                                    "text": "检测到任务中断，已自动恢复执行完成。请等待返回最新状态后继续操作。",
                                })
                                yield _sse("done", {"phase": "recovered"})
                                return
                    except Exception as _exc:
                        print(f"[chat] ❌ astream(None) 恢复失败: {_exc}", flush=True)
                    yield _sse("message", {
                        "text": "检测到任务执行中断，但自动恢复失败。请重开窗口启动新任务。",
                    })
                    yield _sse("done", {"phase": "failed"})
                    return
                # paused → 放行

            # ------------------------------------------------------------------
            # dispatch：四种互斥分支
            # ------------------------------------------------------------------
            if intent == "start_task":
                async for ev in _pipe(_handle_start_task(
                    message, product_images, platform, image_type, marketing_goal, graph,
                    conversation_id=conv_id_for_this_turn,
                )):
                    yield ev

            elif intent == "redo":
                # ------------------------------------------------------------------
                # v2 redo dispatch：
                #   - 只有 1 个允许选项（c1/c2）→ 直接走 backward_to_nodeX（自动执行）
                #   - ≥2 个允许选项（c3/c4）→ 发 SSE selection_required，让前端弹 radio
                # ------------------------------------------------------------------
                allowed = build_redo_options(current_node)
                if len(allowed) == 1:
                    auto_intent = f"backward_to_{allowed[0]['value']}"
                    print(f"[chat] redo dispatch: 仅 1 个选项，自动执行 {auto_intent}", flush=True)
                    async for ev in _pipe(_handle_backward(auto_intent, t_id or '', graph,
                                                           product_images=product_images)):
                        yield ev
                elif len(allowed) > 1:
                    print(f"[chat] redo dispatch: {len(allowed)} 个选项 → 发 selection_required", flush=True)

                    # ⚠️ 必须先持久化 checkpoint，再 yield _sse("done")！
                    # 前端收到 done 后会停止读 SSE → ASGI 关闭连接 → generator 被 asyncio 取消。
                    # 如果 aupdate_state 放在 yield done 之后，它会被 CancelledError 中断
                    # （CancelledError 是 BaseException 子类，except Exception 兜不住），
                    # 导致 checkpoint 里永远没有 phase=waiting_selection，刷新后 redo 卡片丢失。
                    try:
                        graph = _get_graph()
                        config = _langgraph_config(t_id)
                        await graph.aupdate_state(config, {
                            "phase": "waiting_selection",
                            # 复用已注册的 interrupt channel 存 redo 选项
                            "interrupt": {
                                "node": "redo_selection",
                                "schema": {
                                    "mode": "redo_selection",
                                    "options": allowed,
                                },
                                "hint": "请选择要重新执行的步骤",
                            },
                        })
                        print(f"[chat] ✅ checkpoint 已持久化 phase=waiting_selection", flush=True)
                    except BaseException as exc:
                        # CancelledError、Exception 全都捕获打日志，方便确认是否还有其他来源的取消
                        import asyncio as _asio
                        if isinstance(exc, _asio.CancelledError):
                            print(f"[chat] ❌ aupdate_state 被 CancelledError 取消"
                                  f"（graph 可能未初始化或连接提前关闭）", flush=True)
                        else:
                            print(f"[chat] ❌ 持久化 waiting_selection 失败: {exc!r}", flush=True)
                            import traceback as _tb
                            _tb.print_exc()

                    yield _sse("selection_required", {
                        "message": "请选择要重新执行的步骤：",
                        "options": allowed,
                        "task_id": t_id or "",
                    })
                    yield _sse("done", {"phase": "waiting_selection"})
                else:
                    # 无选项（理论上不会，redo 守卫已经挡了无 current_node）
                    yield _sse("message", {"text": "当前阶段不允许重做。"})
                    yield _sse("done", {"phase": "done"})
            elif intent.startswith("backward_to_node"):
                async for ev in _pipe(_handle_backward(intent, t_id or '', graph,
                                                       product_images=product_images)):
                    yield ev
            else:
                async for ev in _pipe(_handle_resume(
                    intent, t_id or '', current_node, intent_result,
                    message, model_images, product_images,
                    existing_report, existing_schemes,
                    existing_model_images, graph,
                    selected_scheme_indices=selected_scheme_indices,
                    graph_state=graph_state,
                )):
                    yield ev
        except Exception as exc:
            import traceback as _tb2
            print(f"[chat] ❌ dispatch error intent={intent}: {exc}", flush=True)
            _tb2.print_exc()
            yield _sse("error", {"phase": "failed", "message": str(exc)})
            yield _sse("done", {"phase": "failed"})

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ===========================================================================
# Handlers
# ===========================================================================


async def _handle_start_task(
    message: str,
    product_images: list[UploadFile],
    platform: str,
    image_type: str,
    marketing_goal: str,
    graph,
    *,
    conversation_id: str | None = None,
) -> AsyncGenerator[str, None]:
    from wellflow.app.utils.image_store import save_upload

    if not product_images:
        yield _sse("message", {"text": "请上传至少 1 张商品图片再开始。"})
        yield _sse("done", {"phase": "done"})
        return

    task_id = _short_uuid()
    q = await drain_and_subscribe(task_id)

    raw_files = [(f.filename or "image", await f.read(), f.content_type) for f in product_images]
    product_image_paths = save_upload(task_id, raw_files, prefix="p")
    image_names = [f.filename for f in product_images]

    # 生成简短 description 供前端历史列表展示
    _desc = message.strip()[:30]
    if not _desc:
        _desc = "识别图片中的商品"

    request_json = {
        "description": _desc,
        "platform": platform,
        "image_type": image_type,
        "marketing_goal": marketing_goal,
        "product_link": None,
        "image_model": None,
        "image_count": len(product_images),
        "has_images": len(product_images) > 0,
        "has_text": bool(message),
        "product_image_names": image_names,
        "product_images": product_image_paths,
    }

    def _sync_write():
        with session_scope() as sdb:
            repo = TaskRepo(sdb)
            repo.create(
                task_id=task_id,
                request_json=request_json,
                phase="input",
                brand_config_json={},
                conversation_id=conversation_id,
            )
            repo.add_event(task_id, "task_created", phase="input", payload_json=request_json)
            # 同步 conversation.current_task_id
            if conversation_id:
                from wellflow.app.repositories.conversation_repo import ConversationRepo as _CR
                _CR(sdb).update_current_task(conversation_id, task_id)
    await asyncio.to_thread(_sync_write)

    config = _langgraph_config(task_id)
    initial_state = {
        "task_id": task_id, "phase": "input",
        "request": request_json, "brand_config": {},
        "node1": {}, "node2": {}, "node3": {},
        "selected_plan_ids": [], "progress": {}, "cost": {},
        "interrupt": None, "error": None, "event_ids": [],
    }

    asyncio.create_task(_start_graph(task_id, graph, config, initial_state=initial_state))

    yield _sse("task_created", {
        "task_id": task_id, "phase": "input", "estimated_cost_range": [2.0, 10.0],
        "description": _desc,
    })
    yield _sse("phase", {"phase": "input"})

    async for ev in _stream_queue(task_id, q):
        yield ev


async def _handle_backward(
    intent: str,
    task_id: str,
    graph,
    *,
    product_images: list[UploadFile] | None = None,
) -> AsyncGenerator[str, None]:
    from wellflow.app.utils.image_store import save_upload

    # ------------------------------------------------------------------
    # 意图 → 回退目标映射（统一 nodeX → resume_node + redo_target）
    #   backward_to_node1 → resume c1, redo node1（只能在 c1 触发，node1 永久锁定）
    #   backward_to_node2 → resume c2, redo node2
    #   backward_to_node3 → resume c3, redo node3
    #   backward_to_node4 → resume c4, redo node4
    # ------------------------------------------------------------------
    _INTENT_MAP = {
        "backward_to_node1": ("c1", "node1", 1),
        "backward_to_node2": ("c2", "node2", 2),
        "backward_to_node3": ("c3", "node3", 3),
        "backward_to_node4": ("c4", "node4", 4),
    }
    if intent not in _INTENT_MAP:
        yield _sse("message", {"text": f"不认识的回退意图: {intent}"})
        yield _sse("done", {"phase": "done"})
        return
    resume_node, redo_target, step_num = _INTENT_MAP[intent]

    # ------------------------------------------------------------------
    # 流程守卫：
    #   1. phase == done → 允许 redo（Command(goto='c4_review_result') 从 finalize 回跳）
    #      原来是硬拦"任务已确认入库，不能再回退重做"——这是历史遗留策略，
    #      用户明确说"重做"时我们应该让 graph 从 END 续跑，而不是拒绝。
    #      LangGraph 的 Command(goto=X) 在 END checkpoint 上会把 goto 当作
    #      "从 X 继续跑"——state 会保留 checkpoint 上已有的 node1/2/3/4 产物。
    #   2. backward_to_node1 仅允许 interrupt_node == c1（node1 永久锁定）
    # ------------------------------------------------------------------
    def _get_state() -> tuple[str | None, str | None]:
        try:
            with session_scope() as db:
                t = TaskRepo(db).get(task_id)
                if not t:
                    return None, None
                node = (t.interrupt_json or {}).get("node") if t.interrupt_json else None
                return t.phase, node
        except Exception:
            return None, None

    current_phase, interrupt_node = await asyncio.to_thread(_get_state)
    # phase=done 不再硬拦——用户明确说"重做"时让 graph 从 END 续跑到 c4_review_result
    if intent == "backward_to_node1" and interrupt_node != "c1":
        print(f"[backward] 🛡️ task={task_id} interrupt={interrupt_node}，"
              f"node1 已锁定（只能在 c1 回退），拒绝 backward_to_node1", flush=True)
        yield _sse("message", {"text": "商品报告已确认进入方案阶段，不能再重做商品识别。"})
        yield _sse("done", {"phase": "done"})
        return

    # 如果用户上传了新商品图 → 落盘 + 更新 request.product_images
    cmd_update: dict[str, Any] = {}
    if product_images:
        raw = [(f.filename or "image", await f.read(), f.content_type) for f in product_images]
        new_paths = save_upload(task_id, raw, prefix="p")
        # 读取旧 request 并 merge（保持 request 中其他字段不变）
        _, graph_state = await _aget_snapshot(task_id)
        old_request = (graph_state or {}).get("request", {}) if graph_state else {}
        merged_request = {**old_request, "product_images": new_paths,
                          "product_image_names": [f.filename for f in product_images],
                          "image_count": len(product_images),
                          "has_images": True}
        cmd_update["request"] = merged_request
        print(f"[backward] 🔄 商品图已更新: {len(new_paths)} 张, intent={intent}", flush=True)

    q = await drain_and_subscribe(task_id)
    config = _langgraph_config(task_id)

    # ------------------------------------------------------------------
    # Command 组装：两种路径
    #   A) graph 停在 cX interrupt → Command(resume=...) 让 _cX_decision 做清理 + 路由
    #      （原有逻辑）
    #   B) graph 已 END（phase=done 或无 interrupt）→ Command(goto=目标执行节点) +
    #      API 层手动做 state 清理（复用 _c4_review_result 里的分层清理规则）
    #      LangGraph 在 END checkpoint 上 goto 会从目标节点继续跑，state 保留 checkpoint 里的内容。
    # ------------------------------------------------------------------
    _EXEC_NODE_OF_TARGET = {
        "node1": "node1_product_analyzer",
        "node2": "node2_planning_scheme",
        "node3": "node3_prompt_generation",
        "node4": "node4_generate_image",
    }

    if current_phase == "done":
        # ── 路径 B：graph 已 END，手动清理 state + goto 到目标执行节点 ──
        # 先读最新 graph_state 来做正确的清理
        _, latest_state = await _aget_snapshot(task_id)
        latest_state = latest_state or {}

        from wellflow.app.workflows.state import cleared
        node1 = latest_state.get("node1", {}) or {}
        node2 = latest_state.get("node2", {}) or {}
        node3 = latest_state.get("node3", {}) or {}
        node4 = latest_state.get("node4", {}) or {}

        redo_target_cleanup: dict[str, dict[str, Any]] = {}
        # 复用 _c4_review_result 里的分层清理规则（node1 已永久锁定，只清 2/3/4）
        if redo_target == "node2":
            redo_target_cleanup = {
                "node2": cleared(),
                "node3": cleared(model_images=node3.get("model_images", []),
                                 ratio=node3.get("ratio"), image_model=node3.get("image_model")),
                "node4": cleared(),
                "phase": "c4_review",
            }
        elif redo_target == "node3":
            redo_target_cleanup = {
                "node3": cleared(model_images=node3.get("model_images", []),
                                 ratio=node3.get("ratio"), image_model=node3.get("image_model")),
                "node4": cleared(),
                "phase": "c4_review",
            }
        else:  # node4
            new_node4 = dict(node4) if isinstance(node4, dict) else {}
            items = new_node4.get("work_items", []) or []
            for it in items:
                if isinstance(it, dict):
                    it["status"] = "pending"
            new_node4["outputs"] = []
            new_node4["failed_items"] = []
            redo_target_cleanup = {
                "node4": new_node4,
                "phase": "c4_review",
            }

        update_dict = {**redo_target_cleanup, **cmd_update} if cmd_update else redo_target_cleanup
        exec_node = _EXEC_NODE_OF_TARGET.get(redo_target, redo_target)
        cmd = Command(goto=exec_node, update=update_dict)
        print(f"[backward] 🎯 graph已END → Command(goto={exec_node}) cleanup_keys={list(redo_target_cleanup.keys())}", flush=True)
    else:
        # ── 路径 A：graph 停在 cX interrupt → Command(resume=...) ──
        # 🔑 redo 必须走 resume 语义：interrupt 节点（_cX_*）的 redo 分支负责清理下游
        # state（cleared() 整体替换）并通过 _route_cX_decision 路由回目标执行节点。
        # 不能用 Command(goto=...)：graph 处于 interrupt 态时 goto 不会跳过当前 interrupt
        # 节点，interrupt() 会以已清空的 state 立刻重新抛出，表现为"秒回一个空报告"。
        resume_values = {"node": resume_node, "decision": "redo", "redo_target": redo_target}
        cmd = Command(resume=resume_values, **({"update": cmd_update} if cmd_update else {}))
        print(f"[backward] 🎯 Command(resume={resume_values}) task={task_id} update_keys={list(cmd_update.keys())}", flush=True)

    def _clear_interrupt():
        try:
            with session_scope() as db:
                repo = TaskRepo(db)
                repo.save_interrupt(task_id, None)
        except Exception:
            pass
    await asyncio.to_thread(_clear_interrupt)

    asyncio.create_task(_start_graph(task_id, graph, config, command=cmd))
    _sync_db_after_jump(task_id, resume_node, [redo_target])

    yield _sse("resume_ack", {
        "task_id": task_id, "node": resume_node,
        "message": f"好的，已回到第 {step_num} 步。",
    })

    async for ev in _stream_queue(task_id, q):
        yield ev


async def _handle_resume(
    intent: str,
    task_id: str,
    current_node: str | None,
    intent_result: dict[str, Any],
    message: str,
    model_images: list[UploadFile],
    product_images: list[UploadFile] | None,
    existing_report: str,
    existing_schemes: list,
    existing_model_images: list[str],
    graph,
    *,
    selected_scheme_indices: str | None = None,
    graph_state: dict[str, Any] | None = None,
) -> AsyncGenerator[str, None]:
    from wellflow.app.utils.image_store import save_upload

    if not current_node:
        yield _sse("message", {"text": "当前没有可继续的节点，请先开始一个任务。"})
        yield _sse("done", {"phase": "done"})
        return

    # ------------------------------------------------------------------
    # 🛡️ 硬性规则校验：绝不允许跳过损坏 / 数据缺失的节点
    #   场景：current_node=c2，但 node2.schemes=[] → 阻断向下流转，
    #   只允许 retry 当前 node2。
    # ------------------------------------------------------------------
    from wellflow.app.graph_context import validate_current_node_products
    ok, missing = validate_current_node_products(current_node, graph_state)
    if not ok:
        print(
            f"[resume] 🛡️ 硬性守卫拦截 current_node={current_node}，"
            f"缺失产物: {missing} → 拒绝 resume，请 retry 当前 node",
            flush=True,
        )
        yield _sse("message", {
            "text": (
                f"当前节点({current_node})产物缺失或损坏({missing})，"
                "请重试当前节点，不允许跳过向下流转。"
            ),
        })
        yield _sse("done", {"phase": "blocked"})
        return

    q = await drain_and_subscribe(task_id)

    model_image_paths: list[str] = []
    if model_images:
        raw = [(f.filename or "model", await f.read(), f.content_type) for f in model_images]
        model_image_paths = save_upload(task_id, raw, prefix="m")

    # 处理 product_images（仅在关键词覆盖时出现，比如 redo→node1 换商品图）
    product_image_paths: list[str] = []
    if product_images:
        raw = [(f.filename or "image", await f.read(), f.content_type) for f in product_images]
        product_image_paths = save_upload(task_id, raw, prefix="p")
        print(f"[resume] 🔄 商品图已更新: {len(product_image_paths)} 张", flush=True)

    node = current_node
    resume_values: dict[str, Any] = {"node": node}

    # ------------------------------------------------------------------
    # 各节点 resume_values 组装
    #   - c1: confirm_current / edit_and_confirm_c1（改报告）→ 走 decision/redo 分支前已处理
    #   - c2: confirm_current → 确认（默认全选或 selected_scheme_indices）
    #   - c3: confirm_current → 确认 prompt（不再有 redo，c3 的重做统一 backward_to_node3）
    #   - c4: confirm_generation / redo_generation → decision + redo_target
    # ------------------------------------------------------------------
    if node == "c1":
        # C1 阶段无需传图，用户直接确认/编辑报告即可继续

        if intent == "edit_and_confirm_c1":
            resume_values["confirmed_report"] = message
        else:
            resume_values["confirmed_report"] = existing_report
        if model_image_paths:
            resume_values["model_images"] = model_image_paths
        resume_values.setdefault("ratio", "9:16竖版")
        resume_values.setdefault("count", 3)

    elif node == "c2":
        # 选中项优先级：前端 checkbox 显式传的 > 意图从消息文本解析的 > 默认全选。
        # 字段名必须是 selected_scheme_indices（_c2_select_scheme 读这个名字），
        # 历史上误写成 selected_prompt_indices 导致 resume 值永远被忽略。
        indices: list[int] = []
        if selected_scheme_indices:
            for part in selected_scheme_indices.split(","):
                part = part.strip()
                if part.isdigit():
                    indices.append(int(part))
        if not indices:
            selected = intent_result.get("selected_indices")
            if selected == "all":
                indices = list(range(len(existing_schemes)))
            elif isinstance(selected, list):
                for x in selected:
                    try:
                        idx = int(x)
                        if 0 <= idx < len(existing_schemes):
                            indices.append(idx)
                    except (ValueError, TypeError):
                        pass
        if not indices:
            indices = list(range(len(existing_schemes)))
        resume_values["selected_scheme_indices"] = indices
        if model_image_paths:
            resume_values["model_images"] = model_image_paths

    elif node == "c3":
        # C3 只有 confirm_current：用户确认 prompt 进入 Node4 生图。
        # C3 下的重做意图（比如"换背景"）已在意图分类阶段归为 backward_to_node3，
        # 由 _handle_backward 单独处理，不会走到这里。
        # 历史上曾有 resume_values["action"] = "redo" 的残留分支，
        # 但 graph 的 _c3_confirm_prompt 读的是 decision 字段而非 action，导致 c3 redo 从未生效。
        # 新设计下此问题已自然消除。
        if model_image_paths:
            resume_values["model_images"] = model_image_paths

    elif node == "c4":
        # C4 独占 confirm_generation / redo_generation 两个意图。
        # 统一走 decision + redo_target（graph 的 _c4_review_result 读这两个字段）。
        if intent == "redo_generation":
            resume_values["decision"] = "redo"
            resume_values["redo_target"] = "node4"  # 默认只重生图，保留上游 node1/2/3
        else:
            # confirm_generation（c4 下所有非 redo 的意图都当作确认）
            resume_values["decision"] = "confirm"

    print(f"[chat] resume_values node={node}: {list(resume_values.keys())}", flush=True)

    def _clear_interrupt():
        try:
            with session_scope() as db:
                repo = TaskRepo(db)
                repo.save_interrupt(task_id, None)
        except Exception:
            pass
    await asyncio.to_thread(_clear_interrupt)

    config = _langgraph_config(task_id)

    # 组装 Command：resume 是传给 interrupt 的值，update 是直接更新 state 的字段
    cmd_kwargs: dict[str, Any] = {"resume": resume_values}
    if product_image_paths:
        # 读取旧 request 并合并（商品图追加合并，超过 3 张取最新）
        _, graph_state = await _aget_snapshot(task_id)
        old_request = (graph_state or {}).get("request", {}) if graph_state else {}

        # 追加合并商品图：旧 + 新，取最后 3 张
        old_products: list[str] = old_request.get("product_images") or []
        merged_products = old_products + product_image_paths
        if len(merged_products) > 3:
            merged_products = merged_products[-3:]

        # 文件名也同步追加
        old_names: list[str] = old_request.get("product_image_names") or []
        new_names = [f.filename for f in (product_images or [])]
        merged_names = old_names + new_names
        if len(merged_names) > 3:
            merged_names = merged_names[-3:]

        merged_request = {
            **old_request,
            "product_images": merged_products,
            "product_image_names": merged_names,
            "image_count": len(merged_products),
            "has_images": True,
        }
        cmd_kwargs["update"] = {"request": merged_request}

    cmd = Command(**cmd_kwargs)

    asyncio.create_task(_start_graph(task_id, graph, config, command=cmd))

    yield _sse("resume_ack", {
        "task_id": task_id, "node": node, "message": "好的，正在继续执行…",
    })

    async for ev in _stream_queue(task_id, q):
        yield ev
