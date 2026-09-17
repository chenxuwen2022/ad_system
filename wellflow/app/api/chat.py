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


async def _aget_graph_state(task_id: str) -> dict[str, Any] | None:
    from wellflow.app.runtime import get_graph as _gg
    g = _gg()
    if g is None:
        return None
    try:
        snapshot = await g.aget_state(_langgraph_config(task_id))
    except Exception as exc:
        print(f"[chat] aget_state 失败 task={task_id}: {exc}", flush=True)
        return None
    if snapshot and hasattr(snapshot, "values"):
        return snapshot.values
    return None


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


def _handle_graph_chunk(task_id: str, chunk: Any) -> None:
    from wellflow.app.event_bus import publish as _eb
    from wellflow.app.graph_persist import persist_interrupt, persist_phase
    interrupt_value = _extract_interrupt_value(chunk)
    if interrupt_value:
        print(f"[chat] ✅ interrupt! node={interrupt_value.get('node')}", flush=True)
        phase = f"{interrupt_value.get('node')}_confirm"
        _eb(task_id, "phase", {"phase": phase})
        _eb(task_id, "interrupt", {**interrupt_value, "_phase": phase})
        asyncio.get_event_loop().run_in_executor(
            None, persist_interrupt, task_id, interrupt_value, phase
        )
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
            asyncio.get_event_loop().run_in_executor(
                None, persist_phase, task_id, phase, node_name
            )
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
            _handle_graph_chunk(task_id, chunk)
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
            print(f"[chat:stream] task={task_id} ← {etype}", flush=True)

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
    existing_prompts: list[str] = []
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
        interrupt = task.interrupt_json or {}
        current_node = interrupt.get("node")
        # 兜底：如果 interrupt_json 缺失（异常中断），从 DB phase 反推
        if not current_node and task.phase:
            _PHASE_TO_NODE = {
                "c1_confirm": "c1",
                "c2_confirm": "c2",
                "c3_confirm": "c3",
                "c4_generating": "c4",
            }
            current_node = _PHASE_TO_NODE.get(task.phase)
            if current_node:
                print(f"[chat] interrupt_json 缺失，从 phase={task.phase} 反推 node={current_node}", flush=True)
        graph_state = await _aget_graph_state(t_id)
        if graph_state:
            completed_mask = compute_completed_mask(graph_state, current_node)
            existing_report = str(graph_state.get("node1", {}).get("product_insight", "") or "")
            existing_prompts = list(graph_state.get("node2", {}).get("generate_prompts", []) or [])
            existing_model_images = list(graph_state.get("node3", {}).get("model_images", []) or [])
        print(f"[chat] 上下文: task={t_id} node={current_node} completed={completed_mask}"
              f" model_images_in_state={len(existing_model_images)}"
              f" conversation={resolved_conv_id}", flush=True)

    # 🔑 统一 images → 后端判断分流
    product_images, model_images = classify_images(
        images, has_task=has_task, current_node=current_node, message=message,
    )

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

            if intent == "start_task":
                async for ev in _pipe(_handle_start_task(
                    message, product_images, platform, image_type, marketing_goal, graph,
                    conversation_id=conv_id_for_this_turn,
                )):
                    yield ev
            elif intent in ("backward_to_c1", "backward_to_c2", "backward_to_c3"):
                async for ev in _pipe(_handle_backward(intent, t_id or '', graph,
                                                       product_images=product_images)):
                    yield ev
            else:
                async for ev in _pipe(_handle_resume(
                    intent, t_id or '', current_node, intent_result,
                    message, model_images, product_images,
                    existing_report, existing_prompts,
                    existing_model_images, graph,
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

    if intent == "backward_to_c1":
        # 跳到 Node1 执行节点（不是 interrupt 节点），跑完 VLM 自动流到 c1_confirm
        goto_node = "node1_product_analyzer"
        goto_clean = "c1"
        clean_update: dict[str, Any] = {
            "node1": {}, "node2": {}, "node3": {},
            "phase": "c1_confirm", "interrupt": None,
        }
        step_num = 2
    elif intent == "backward_to_c2":
        # 跳到 Node2 执行节点
        goto_node = "node2_planning_scheme"
        goto_clean = "c2"
        clean_update: dict[str, Any] = {
            "node3": {}, "phase": "c2_confirm", "interrupt": None,
        }
        step_num = 4
    else:  # backward_to_c3
        # 跳到 Node3 执行节点
        goto_node = "node3_prompt_generation"
        goto_clean = "c3"
        clean_update: dict[str, Any] = {
            "node3": {}, "phase": "c3_confirm", "interrupt": None,
        }
        step_num = 6

    # 如果用户上传了新商品图 → 落盘 + 更新 request.product_images
    if product_images:
        raw = [(f.filename or "image", await f.read(), f.content_type) for f in product_images]
        new_paths = save_upload(task_id, raw, prefix="p")
        # 读取旧 request 并 merge（保持 request 中其他字段不变）
        graph_state = await _aget_graph_state(task_id)
        old_request = (graph_state or {}).get("request", {}) if graph_state else {}
        merged_request = {**old_request, "product_images": new_paths,
                          "product_image_names": [f.filename for f in product_images],
                          "image_count": len(product_images),
                          "has_images": True}
        clean_update["request"] = merged_request
        print(f"[backward] 🔄 商品图已更新: {len(new_paths)} 张, intent={intent}", flush=True)

    q = await drain_and_subscribe(task_id)
    config = _langgraph_config(task_id)

    # 🔑 LangGraph 1.2.x 的 Command 原生支持 update + goto 原子提交。
    # 旧写法：先 aupdate_state(clean_update) 再 Command(goto=...)，在 LangGraph 1.2 里
    # 会把两次写入折叠到同一 superstep，LastValue channel 收到 2 个值直接炸 InvalidUpdateError。
    # 新写法：Command(update=clean_update, goto=goto_node) —— update 和 goto 在同一条
    # Command 里原子落地，子图节点的输出写入落到下一个 superstep，完美避免双写冲突。
    cmd = Command(update=clean_update, goto=goto_node)
    print(f"[backward] 🎯 Command(update=..., goto={goto_node}) task={task_id} keys={list(clean_update.keys())}", flush=True)

    def _clear_interrupt():
        try:
            with session_scope() as db:
                repo = TaskRepo(db)
                repo.save_interrupt(task_id, None)
        except Exception:
            pass
    await asyncio.to_thread(_clear_interrupt)

    asyncio.create_task(_start_graph(task_id, graph, config, command=cmd))
    _sync_db_after_jump(task_id, goto_clean, list(clean_update.keys()))

    yield _sse("resume_ack", {
        "task_id": task_id, "node": goto_clean,
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
    existing_prompts: list[str],
    existing_model_images: list[str],
    graph,
) -> AsyncGenerator[str, None]:
    from wellflow.app.utils.image_store import save_upload

    if not current_node:
        yield _sse("message", {"text": "当前没有可继续的节点，请先开始一个任务。"})
        yield _sse("done", {"phase": "done"})
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
        selected = intent_result.get("selected_indices")
        if selected == "all":
            resume_values["selected_prompt_indices"] = list(range(len(existing_prompts)))
        elif isinstance(selected, list):
            indices: list[int] = []
            for x in selected:
                try:
                    idx = int(x) if isinstance(x, str) else int(x)
                    if 0 <= idx < len(existing_prompts):
                        indices.append(idx)
                except (ValueError, TypeError):
                    pass
            if indices:
                resume_values["selected_prompt_indices"] = indices
        if "selected_prompt_indices" not in resume_values:
            resume_values["selected_prompt_indices"] = list(range(len(existing_prompts)))
        if model_image_paths:
            resume_values["model_images"] = model_image_paths

    elif node == "c3":
        if intent == "redo_generation":
            resume_values["action"] = "redo"
        else:
            resume_values["action"] = "confirm"
        if model_image_paths:
            resume_values["model_images"] = model_image_paths

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
        graph_state = await _aget_graph_state(task_id)
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
