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
    summarize_graph_state,
    normalize_instruction,
)

router = APIRouter(prefix="/chat", tags=["对话"])


def _short_uuid() -> str:
    return uuid.uuid4().hex[:12]


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json_mod.dumps(data, ensure_ascii=False)}\n\n"


def _node1_locked_message() -> str:
    """产品报告已锁定时的统一固定回复文案。

    按规格：报告已确认后任何入口（聊天 / 节点回退 / 直接 API）
    都返回这句话，引导用户新建任务，不改变已确认报告。
    """
    return (
        "产品报告已确认并锁定，本任务内无法再修改。"
        "如需调整商品信息，请新建任务重新生成报告。"
    )


def _is_node1_locked(graph_state: dict | None) -> bool:
    """共享守卫：从 LangGraph checkpoint state 判断 Node1 报告是否已锁定。"""
    if not isinstance(graph_state, dict):
        return False
    node1 = graph_state.get("node1") or {}
    return bool(node1.get("report_locked"))


def _is_last_refine_succeeded(graph_state: dict | None, target_node: str) -> bool:
    """判断上一轮 refine 是否成功产出了有效数据。

    _refine_history 是在 backward 阶段就 append 的（parent_graph.py 的 c1/c2/c3 分支），
    不管 refine 节点本身执行成功还是报错都会写入 history。所以 history 里有指令
    ≠ refine 成功。必须检查目标节点产物是否存在且非空才能确认。

    🔴 _refine_history 已按 node 隔离为 dict 格式，且已通过 build_refine_history_update 写入。
    本函数只查产物是否存在，不依赖 _refine_history 的旧 flat list 格式。

    Args:
        graph_state: LangGraph checkpoint state
        target_node: "node1" / "node2" / "node3"

    Returns:
        True  = 上一轮 refine 成功，产物有效，重复指令应该 block
        False = 上一轮 refine 没成功（产物为空/被 bug 清掉/类型错误），允许重新执行
    """
    if not isinstance(graph_state, dict):
        return False
    if target_node == "node1":
        node1 = graph_state.get("node1") or {}
        # 有产品洞察文本就算成功（refine_node1 会重写 product_insight）
        return bool(node1.get("product_insight"))
    if target_node == "node2":
        node2 = graph_state.get("node2") or {}
        schemes = node2.get("schemes")
        # 必须是 list 且 len > 0（排除被 schema 对齐 bug 清空成 dict / 空 list 的情况）
        return isinstance(schemes, list) and len(schemes) > 0
    if target_node == "node3":
        node3 = graph_state.get("node3") or {}
        prompts = node3.get("generate_prompts")
        return isinstance(prompts, list) and len(prompts) > 0
    # node4 不支持 refine，走到这里默认返回 True 不 block
    return True


def _quick_report_hash(text: str | None) -> str:
    """chat.py 内部版本绑定 hash（FNV-1a，与 parent_graph._report_hash 保持一致）。"""
    if not text:
        return "0" * 16
    h = 0xCBF29CE484222325
    for ch in text.encode("utf-8"):
        h ^= ch
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


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
# 图片分类规则：images → (product_images, ref_images)
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
) -> tuple[list[UploadFile], dict[str, list[UploadFile]]]:
    """把统一的 images 列表分流为 (product_images, ref_images)。

    ref_images = {"mannequin": [...], "scene": [...], "outfit": [...]}
    这里只区分 product vs 其他；其他默认归 mannequin（旧前端自动上传逻辑保留）。
    新前端应直接按 type 分组后通过 resume_json.reference_images 显式传入，不走这里。

    三层优先级：
      1. message 关键词覆盖（命中 PRODUCT → 全部 product；命中 MODEL → 全部 mannequin）
      2. 上下文路由（无 task → product；有 task + c1/c2/c3 → ref_images.mannequin）
      3. 兜底 → ref_images.mannequin
    """
    if not images:
        return [], {"mannequin": [], "scene": [], "outfit": []}

    msg = message.strip()
    _empty_ref: dict[str, list[UploadFile]] = {"mannequin": [], "scene": [], "outfit": []}

    # 第二层：关键词覆盖
    if _match_any(msg, _PRODUCT_KEYWORDS):
        print(f"[classify_images] 关键词覆盖 → product ({len(images)} 张)", flush=True)
        return list(images), _empty_ref
    if _match_any(msg, _MODEL_KEYWORDS):
        print(f"[classify_images] 关键词覆盖 → mannequin ({len(images)} 张)", flush=True)
        ref_mannequin = {"mannequin": list(images), "scene": [], "outfit": []}
        return [], ref_mannequin

    # 第一层：上下文路由
    if not has_task:
        print(f"[classify_images] 无任务 → product ({len(images)} 张)", flush=True)
        return list(images), _empty_ref
    if current_node in ("c1", "c2"):
        print(f"[classify_images] 上下文 c={current_node} → product ({len(images)} 张)", flush=True)
        return list(images), _empty_ref
    if current_node in ("c3", "c4"):
        print(f"[classify_images] 上下文 c={current_node} → mannequin ({len(images)} 张)", flush=True)
        ref_mannequin = {"mannequin": list(images), "scene": [], "outfit": []}
        return [], ref_mannequin

    # 兜底
    print(f"[classify_images] 兜底 → product ({len(images)} 张)", flush=True)
    return list(images), _empty_ref


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
        from wellflow.app.llm.model_pool import prepare_task_models
        await prepare_task_models(task_id)
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
    if event_type == "message":
        return _sse("message", event_data)
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
    if event_type == "node4_image_done":
        return _sse("node4_image_done", event_data)
    if event_type == "node4_image_failed":
        return _sse("node4_image_failed", event_data)
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
    sku_id: int | None = Form(default=None),
    platform: str = Form(default="taobao"),
    image_type: str = Form(default="ad"),
    marketing_goal: str = Form(default="acquisition"),
    # c2 界面 checkbox 选中的方案索引（逗号分隔，如 "0,2"），前端输入框发消息时带上
    selected_scheme_indices: str | None = Form(default=None),
    scheme_prompt_counts: str | None = Form(default=None),
    reference_images: str | None = Form(default=None),
    # 前端 dispatch 层回传：用户显式指定要微调哪一步（node1/node2/node3）
    # 仅作 LLM 意图分类的"强引导"上下文，绝不绕过 LLM
    selected_finetuning_target: str | None = Form(default=None),
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

    # SKU is chosen explicitly; task/conversation identities may not disagree.
    from wellflow.app.models.asset_models import ProductSku
    from wellflow.app.models.task_models import Task
    binding_repo = ConversationRepo(db)
    bound_task = db.get(Task, task_id) if task_id else None
    bound_conv = binding_repo.resolve(conversation_id) if conversation_id else None
    if bound_task and bound_task.conversation_id:
        task_conv = binding_repo.get(bound_task.conversation_id)
        if conversation_id and (not bound_conv or bound_conv.conversation_id != bound_task.conversation_id):
            raise HTTPException(409, "任务不属于当前对话")
        bound_conv = task_conv
    if bound_conv:
        if sku_id is not None and sku_id != bound_conv.sku_id:
            raise HTTPException(409, "对话关联商品已锁定，请新建对话或先绑定历史对话")
        sku_id = bound_conv.sku_id
    if sku_id is not None and db.get(ProductSku, sku_id) is None:
        raise HTTPException(404, "关联商品不存在")
    if not bound_conv and sku_id is None:
        raise HTTPException(422, "请先选择 SKU 商品")

    if bound_task and bound_task.phase == "archive_pending":
        raise HTTPException(409, "图片入库待完成，请用原选择重试入库")
    has_task = bool(task_id)
    t_id: str | None = None
    current_node: str | None = None
    task = None  # Task ORM object（仅 has_task=True 时赋值）
    graph_state: dict | None = None  # LangGraph checkpoint state（仅 has_task=True 时赋值）
    completed_mask = [False] * 6
    existing_report = ""
    existing_schemes: list = []
    existing_ref_images: dict[str, list[str]] = {"mannequin": [], "scene": [], "outfit": []}

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
            # 六位依次表示 node1 产物/c1 确认、node2 产物/c2 确认、
            # node3 产物/c3 确认；确认位必须读取 checkpoint 中的确认记录。
            completed_mask = [False] * 6
            confirmations = graph_state.get("confirmations") or {}
            if graph_state.get("node1", {}).get("product_insight"):
                completed_mask[0] = True
            completed_mask[1] = bool(confirmations.get("c1"))
            if graph_state.get("node2", {}).get("schemes") or current_node in ("c2", "c3", "c4"):
                completed_mask[2] = True
            completed_mask[3] = bool(confirmations.get("c2"))
            if graph_state.get("node3", {}).get("outputs") or current_node == "c3":
                completed_mask[4] = True
            completed_mask[5] = bool(confirmations.get("c3"))

            existing_report = str(graph_state.get("node1", {}).get("product_insight", "") or "")
            # c2 的可选项是 schemes（方案），不是 prompts —— 历史上误读 node2.generate_prompts
            # （该字段属于 node3），导致 existing_schemes 永远为空，"默认全选"退化成"全不选"
            existing_schemes = list(graph_state.get("node2", {}).get("schemes", []) or [])
            existing_ref_images = graph_state.get("node3", {}).get("reference_images") or existing_ref_images
        print(f"[chat] 上下文: task={t_id} node={current_node} completed={completed_mask}"
              f" ref_images={existing_ref_images}"
              f" conversation={resolved_conv_id}", flush=True)

    # ------------------------------------------------------------------
    # 🛑 提前拦截：纯文本重复 refine 指令（在 LLM classify() 之前）
    #
    # 只有当用户明确在说同一条微调指令时才会命中——完全相同的指令在 refine_history[-1] 里。
    # 命中就直接返回 SSE + persist，跳过 classify()，省一次 LLM 调用。
    #
    # 只做精确 normalize 匹配，不做"语义近似"检测（避免误杀真正想重提的指令）。
    # 这层之后 event_generator 里 edit 分支的重复检测还会再兜一次，
    # 捕捉 LLM 改写过的 refine_instruction 与历史重复的情况。
    # ------------------------------------------------------------------
    _PHASE_CN_MAP = {"node1": "c1_confirm", "node2": "c2_select",
                     "node3": "c3_confirm", "node4": "c4_review"}
    _TARGET_CN_MAP = {"node1": "报告", "node2": "方案", "node3": "提示词", "node4": "生图"}
    if has_task and graph_state and message:
        from wellflow.app.workflows.state import get_node_refine_history
        _FALLBACK_NODE = {"c1": "node1", "c2": "node2",
                          "c3": "node3", "c4": "node4"}
        _tgt = _FALLBACK_NODE.get(current_node or "", "node2")
        # 🔴 重复检测也必须按 node 隔离 —— node2 的历史不能误拦截 node3 的同名字指令
        _h = get_node_refine_history(graph_state, _tgt)
        if _h and isinstance(_h[-1], str) and normalize_instruction(message) == normalize_instruction(_h[-1]):
            # 指令重复 → 还要看上一轮 refine 是否真的成功产出了数据
            # 如果上一轮 refine 因为 bug/异常没成功（产物为空或被清掉），就不能 block，
            # 必须允许用户重新执行，否则用户永远卡在"等待确认"
            if not _is_last_refine_succeeded(graph_state, _tgt):
                print(f"[chat] 🔄 指令重复但上一轮 refine 未成功（产物为空），放行让它重新执行 target={_tgt}", flush=True)
            else:
                print(f"[chat] 🛑 提前拦截: raw message 与上一条 refine_history[-1] 完全重复 → 跳过 classify()", flush=True)
                _lock_text = (
                    f"这条微调指令和上一轮完全一样哦，上一轮已经对{_TARGET_CN_MAP.get(_tgt, '产物')}做过相同的修改了。"
                    "如果想继续调整，可以换一条不一样的指令～"
                )

                async def _dup_sse():
                    _chunk = _sse("message", {"text": _lock_text})
                    # 直接在 async 函数里 inline persist assistant（不能引用 event_generator 内部的闭包辅助函数）
                    if resolved_conv_id:
                        try:
                            from wellflow.app.repositories.conversation_repo import ChatMessageRepo as _CMR
                            with session_scope() as _sdb2:
                                _CMR(_sdb2).create(
                                    conversation_id=resolved_conv_id, role="assistant",
                                    text=_lock_text, task_id=t_id,
                                )
                                _CR = ConversationRepo(_sdb2)
                                _CR.touch(resolved_conv_id)
                        except Exception:
                            pass
                    yield _chunk
                    yield _sse("done", {"phase": _PHASE_CN_MAP.get(_tgt, "done")})

                # 必须先把 conversation 关联和用户消息持久化好——这是后续所有 handler 的隐含前提
                conv_repo = ConversationRepo(db)
                msg_repo = ChatMessageRepo(db)
                resolved_conv_id: str | None = conversation_id
                if t_id and not resolved_conv_id:
                    try:
                        _tk = TaskRepo(db).get(t_id)
                        if _tk and _tk.conversation_id:
                            resolved_conv_id = _tk.conversation_id
                    except Exception:
                        pass
                if resolved_conv_id:
                    try:
                        msg_repo.create(
                            conversation_id=resolved_conv_id,
                            role="user", text=message,
                            images_json=[], intent="edit", task_id=t_id,
                        )
                        conv_repo.touch(resolved_conv_id)
                    except Exception:
                        pass
                return StreamingResponse(_dup_sse(), media_type="text/event-stream")

    # 🔑 统一 images → 后端判断分流
    product_images, ref_images = classify_images(
        images, has_task=has_task, current_node=current_node, message=message,
    )

    # ------------------------------------------------------------------
    # 🛑 LLM 意图分类短路：无任务 + 无文字消息 → 必定是 start_task
    #
    # 场景：用户新建任务时只传了图片（产品图），前端没发任何文字。
    # 这时根本不需要调 LLM 意图分类器——没有文字可供分析，
    # 唯一合理的意图就是 start_task。省掉一次 LLM 调用 + 几百 ms 延迟。
    #
    # 判定条件（必须同时满足）：
    #   - has_task=False （真的没任务，不是"任务未启动"）
    #   - message 为空 / 全空白（strip 后长度 == 0）
    #   - len(images) > 0（有图片，图片已在 classify_images 里分流完）
    # ------------------------------------------------------------------
    if (not has_task) and (not message) and images:
        print(f"[chat] 🛑 意图分类短路：无任务 + 无文字 + 图片={len(images)}张 → 直接 start_task（跳过 LLM）", flush=True)
        intent_result: dict[str, Any] = {
            "intent": "start_task",
            "reasoning": "无任务 + 无文字消息 + 有图片 → 必然新建任务，短路跳过 LLM",
            "refine_target": None,
            "refine_instruction": None,
            "selected_indices": None,
            "blocked_step": None,
            "_short_circuited": True,  # 调试标记：下游 dispatch 可忽略
        }
        intent = "start_task"
    else:
        # ------------------------------------------------------------------
        # 意图分类（全 LLM，唯一入口）
        # ------------------------------------------------------------------
        _state_brief = summarize_graph_state(graph_state)
        # 日志打印：结构摘要（一眼看清各 node 产物状态）+ 完整 brief 仅在 debug 时打
        # 旧版 print(_state_brief[:300]) 会全是 node1 报告正文，c2/c3/c4 阶段完全看不到当前产物
        if graph_state:
            _sn1 = graph_state.get("node1") or {}
            _sn2 = graph_state.get("node2") or {}
            _sn3 = graph_state.get("node3") or {}
            _sn4 = graph_state.get("node4") or {}
            _n1_tag = f"locked(len={len(_sn1.get('product_insight',''))})" if _sn1.get("report_locked") else f"unlocked(len={len(_sn1.get('product_insight',''))})" if _sn1.get("product_insight") else "none"
            _schemes = _sn2.get("schemes") or []
            _sel = _sn2.get("selected_scheme_indices") or []
            _n2_tag = f"{len(_schemes)}套(已选{_sel})" if _schemes else "none"
            _prompts = _sn3.get("generate_prompts") or []
            _details = _sn3.get("prompts_detail") or []
            _n3_tag = f"{len(_prompts)}条" if _prompts else f"{len(_details)}条(detail)" if _details else "none"
            _outputs = _sn4.get("outputs") or []
            _works = _sn4.get("work_items") or []
            _n4_tag = f"{len(_outputs)}张" if _outputs else f"进行中{sum(1 for w in _works if isinstance(w,dict) and w.get('status')=='done')}/{len(_works)}" if _works else "none"
            print(f"[chat] 📋 state 结构摘要: node1={_n1_tag}, node2={_n2_tag}, node3={_n3_tag}, node4={_n4_tag} | current_node={current_node}", flush=True)
        else:
            print(f"[chat] 📋 state 结构摘要: graph_state=None | current_node={current_node}", flush=True)
        intent_result = await classify(
            message,
            has_task=has_task,
            current_node=current_node,
            completed_mask=completed_mask,
            has_images=len(images) > 0 or (not has_task and sku_id is not None),
            selected_finetuning_target=selected_finetuning_target,
            graph_state_brief=_state_brief,
            task_id=t_id,
        )
        intent = intent_result.get("intent", "chat_outside")
        print(f"[chat] 🎯 LLM 分类结果: intent={intent} refine_target={intent_result.get('refine_target')} "
              f"reason={intent_result.get('reasoning', '')[:80]} | user_msg={message[:60]}", flush=True)

    # ------------------------------------------------------------------
    # persist 辅助函数 —— 必须在 node1 锁定守卫之前定义，
    # 因为 node1 锁定守卫的 early-return SSE 生成器会捕获它们
    # conv_id_for_this_turn 在此处提前声明（后续 start_task 分支会更新），
    # 确保 persist 函数闭包引用的变量在任何执行路径下都有绑定值
    # ------------------------------------------------------------------
    conv_id_for_this_turn: str | None = resolved_conv_id

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
          - event: error         → data.message / data.error

        注意：resume_ack 是纯进度 ack（"好的，正在继续执行…"这类），
        只用于 SSE 即时反馈，**不持久化**到对话历史——否则会在继续后留下误导性消息。
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
        elif ev_type == "error":
            text = str(data.get("message") or data.get("error") or "").strip()
        if text:
            await _persist_assistant_msg(text, known_task_id)

    # ------------------------------------------------------------------
    # 🛡️ Node1 报告锁定守卫：唯一允许的 block。
    # graph_state 显示 node1.report_locked=True 时，任何试图修改/微调 node1 的
    # 入口都被拦截——产品报告一经 c1 确认即永久锁定，不能在后续步骤重做。
    #
    # 两层判定（最安全的写法：只要 node1 已锁定 + 用户消息含 node1 产物锚点词，
    # 或 LLM 明确判了 refine_target=node1，就拦——完全绕开 LLM 可能的意图误判）：
    #   1) LLM refine_target 直接判了 node1 —— 最常见路径
    #   2) 用户消息含 node1 锚点词（"报告"/"洞察报告"/"商品识别报告"…）——
    #      这是对 LLM 分类错误的**最后一道**防御：
    #      例：LLM 把 c3 用户的"修改洞察报告"误判为 confirm_current，
    #      整个 intent == "edit" 分支都绕开 → 守卫兜底：只要消息里含 node1 锚点词就拦
    #
    # 例外：start_task（新任务不应被锁拦住）和空消息（纯 confirm 不含锚点词）
    # ------------------------------------------------------------------
    _node1_locked_here = _is_node1_locked(graph_state)
    _refine_tgt = intent_result.get("refine_target")

    # node1 产物锚点词 —— 只保留**无歧义**的产物名，不跨 node 复用的字段
    # （注意：不要加"品牌调性"/"品牌定位"这类 node2 方案里也可能出现的字段名，
    #    chat.py 这里没有消歧逻辑，加了会导致 node2 refine 被误杀）
    _NODE1_PRODUCT_HINTS = (
        "报告", "商品识别", "商品报告", "识别报告", "洞察报告",
    )
    _msg_has_node1_hint = any(h in message for h in _NODE1_PRODUCT_HINTS)

    # 唯一例外：start_task（用户开新任务，node1 锁定与此无关）
    _is_new_task = intent == "start_task"

    # 命中任一 → 拦（已排除 start_task）
    # 🔴 防御性修正：如果 LLM 已经明确判了 refine_target=node2/node3/node4，
    # 就算 _msg_has_node1_hint 里碰巧有个"报告"字样也绝不能拦——
    # LLM 有 graph_state_brief（含 node1 是否已锁定、node2 有几套方案）做依据，
    # 比关键词匹配可靠得多。只有在 LLM 没给 refine_target 或给了 node1 时，
    # _msg_has_node1_hint 这个兜底才生效。
    _refine_tgt_is_upstream = _refine_tgt in ("node2", "node3", "node4")
    _hits_node1_modification = (
        (not _is_new_task)
        and _refine_tgt == "node1"   # 路径 A：LLM 明确判了 node1 → 必拦
        or (
            (not _is_new_task)
            and _msg_has_node1_hint  # 路径 B：LLM 没给 refine_target 或给了 node1，
            and not _refine_tgt_is_upstream  # 且消息里有 node1 锚点词 → 兜底拦
        )
    )
    if _node1_locked_here and _hits_node1_modification:
        _why_parts = [f"refine_target={_refine_tgt}", f"intent={intent}"]
        if _msg_has_node1_hint and _refine_tgt != "node1":
            _why_parts.append("用户消息含 node1 锚点词，LLM 可能误判意图")
        print(f"[chat] 🛡️ node1 报告已锁定，拦截 intent={intent} | " + " | ".join(_why_parts), flush=True)
        async def _node1_locked_sse():
            chunk = _sse("message", {"text": _node1_locked_message()})
            await _persist_sse_text(chunk, known_task_id=t_id)
            yield chunk
            yield _sse("done", {"phase": "c1_locked"})
        return StreamingResponse(_node1_locked_sse(), media_type="text/event-stream")

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
    if intent == "start_task":
        # 构造 title_hint —— 让 repo 在 create 时根据意图生成真实 title
        _title_hint = {
            "kind": "chat_start_task",
            "message": message,
            "intent": intent,
        }
        # 优先复用前端传的 conversation_id（前端用 createId() 生成，全局唯一）
        if not conv_id_for_this_turn:
            conv_id_for_this_turn = _short_uuid()
            conv_repo.create(
                conversation_id=conv_id_for_this_turn,
                title="新对话",  # 占位，repo 会用 hint 覆盖
                title_hint=_title_hint,
                sku_id=sku_id,
            )
            print(f"[chat] ✨ 新建 conversation={conv_id_for_this_turn} title_hint=start_task", flush=True)
        else:
            # 前端传了但还没建（首次 start_task，前端 generate 的 id 后端还没记录）
            # 用 resolve() 兼容 short_id / 完整 UUID
            existing = conv_repo.resolve(conv_id_for_this_turn)
            if not existing:
                conv_repo.create(
                    conversation_id=conv_id_for_this_turn,
                    title="新对话",
                    title_hint=_title_hint,
                    sku_id=sku_id,
                )
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
        #
        # 🔴 注意：下面 chat_outside / redo_blocked 分支里有
        # `intent = "edit"` 赋值 —— 必须 nonlocal，否则 Python 3 会把 intent
        # 当成 event_generator 的局部变量，遮蔽外层闭包 intent，导致
        # 前面 `if intent == "model_pool_unavailable"` 触发 UnboundLocalError。
        nonlocal intent

        # ── conversation title 推送 ──
        # 新建 conversation 后 repo 已经生成真实 title，推给前端让侧边栏实时更新
        # 只推一次（不影响后续任何 early-return 分支）
        if conv_id_for_this_turn:
            try:
                _cur = conv_repo.resolve(conv_id_for_this_turn)
                if _cur and _cur.title and _cur.title not in ("新对话", ""):
                    yield _sse("conversation_title", {
                        "conversation_id": _cur.conversation_id,
                        "title": _cur.title,
                    })
                    print(f"[chat] 📢 conversation_title={_cur.title}", flush=True)
            except Exception as exc:
                print(f"[chat] ⚠️ conversation_title 推送失败（不阻断）: {exc}", flush=True)

        # ── 模型池全挂：给用户准确的信息，不要误导为闲聊 ──
        if intent == "model_pool_unavailable":
            _reasoning = intent_result.get("reasoning", "")
            print(f"[chat] 🛑 模型池不可用，直接返回准确信息给用户: {_reasoning[:80]}", flush=True)
            chunk = _sse("message", {
                "text": "当前模型池中的大模型均不可用，请稍后再试。",
            })
            await _persist_sse_text(chunk, known_task_id=t_id)
            yield chunk
            yield _sse("done", {"phase": "model_unavailable"})
            return

        if intent == "chat_outside":
            # 🛡️ 防御性兜底：LLM 误判 chat_outside 的最后一道保险
            # 只要 has_task=True + current_node 非空 + 消息里含产物/字段关键词，
            # 就降级为 edit（LLM 意图分类器对"带字段关键词的疑问句"很容易误判为闲聊）。
            _PRODUCT_FIELD_HINTS = (
                "报告", "商品识别", "商品报告", "识别报告", "洞察报告",
                "产品规格", "品牌调性", "核心卖点", "目标受众", "产品细节",
                "商拍方案", "方案", "视觉主题", "场景设定", "模特气质", "光影",
                "提示词", "prompt", "构图", "镜头", "画面",
                "图片", "图像", "生图",
            )
            _msg_mentions_product = any(h in message for h in _PRODUCT_FIELD_HINTS)
            if has_task and current_node and _msg_mentions_product:
                _FALLBACK_NODE = {"c1": "node1", "c2": "node2",
                                  "c3": "node3", "c4": "node4"}
                _fallback_tgt = _FALLBACK_NODE.get(current_node, "node2")
                print(f"[chat] 🛡️ chat_outside 防御兜底：has_task={has_task} current_node={current_node} "
                      f"消息含产物关键词 → 降级为 edit + refine_target={_fallback_tgt}, msg={message[:60]}", flush=True)
                intent = "edit"
                intent_result["refine_target"] = _fallback_tgt
                intent_result["refine_instruction"] = message.strip()
            else:
                chunk = _sse("message", {"text": "暂不支持与生图无关的对话。"})
                await _persist_sse_text(chunk, known_task_id=t_id)
                yield chunk
                yield _sse("done", {"phase": "done"})
                return

        # redo_blocked / skip_forward：被工作流规则 block 的意图
        # redo_blocked + refine_target in (node1, None) → 拦截（node1 已锁定）
        # redo_blocked + refine_target in (node2, node3, node4) → LLM 分类失误，
        #   用户想回到未锁定的步骤 → 转成 edit + refine_target=X 走 refine 路径
        # skip_forward → 试图跳过工作流步骤，一律拦截
        # 用 _dispatch_intent 表示 dispatch 层实际路由的意图；
        # 不改闭包外的 intent（Python 闭包禁止先读后 nonlocal）。
        _dispatch_intent = intent
        if intent == "redo_blocked":
            if _refine_tgt in ("node2", "node3", "node4"):
                # 用户想回到未锁定的步骤 → 降级为 edit 走 refine 路径
                _dispatch_intent = "edit"
                intent_result["refine_target"] = _refine_tgt
                intent_result["refine_instruction"] = intent_result.get("refine_instruction") or message
                print(f"[chat] 🔄 redo_blocked+refine_target={_refine_tgt} → 降级为 edit，走 refine 路径", flush=True)
                # fall through 到运行状态守卫检查后继续 dispatch
            else:
                # node1 锁定守卫应该已拦截；这里兜底
                _msg = _node1_locked_message() if _node1_locked_here else \
                    "当前步骤已完成，如需重新开始请新建任务。"
                print(f"[chat] 🛡️ redo_blocked → {_msg}", flush=True)
                chunk = _sse("message", {"text": _msg})
                await _persist_sse_text(chunk, known_task_id=t_id)
                yield chunk
                yield _sse("done", {"phase": "redo_blocked"})
                return
        if intent == "skip_forward":
            chunk = _sse("message", {
                "text": "当前工作流暂不支持跳过步骤，请按顺序完成后再继续。",
            })
            await _persist_sse_text(chunk, known_task_id=t_id)
            yield chunk
            yield _sse("done", {"phase": "skip_blocked"})
            return

        # ------------------------------------------------------------------
        # 运行状态守卫：同一个 task 并发请求 → 直接返回，防止 graph 冲突
        # （这是唯一保留的安全守卫——不是业务 block，是防止 graph 崩溃）
        # ------------------------------------------------------------------
        if has_task and t_id and _dispatch_intent != "start_task" and is_running(t_id):
            print(f"[chat] 🛡️ task={t_id} 正在执行中，拒绝 intent={_dispatch_intent}", flush=True)
            chunk = _sse("message", {"text": "任务正在执行中，请等待当前操作完成后再试。"})
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
            #   paused  → 放行
            #   running → 拦截（graph 真在执行）
            #   stale   → 尝试 astream(None) 续跑；恢复失败则拦截
            #   done    → 放行（edit 意图会被 dispatch 到 _handle_backward 走路径 B）
            # ------------------------------------------------------------------
            _NEEDS_RUNTIME_CHECK = (
                "confirm_current", "confirm_generation", "edit",
            )
            if has_task and _dispatch_intent in _NEEDS_RUNTIME_CHECK:
                from wellflow.app.graph_context import check_graph_runtime_state
                _rt_state, _rt_age = check_graph_runtime_state(snapshot)
                if _rt_state == "done":
                    print(f"[chat] ✅ graph已END(done)，放行 intent={_dispatch_intent}", flush=True)
                elif _rt_state == "running":
                    print(f"[chat] 🛡️ graph 正在执行（checkpoint 年龄 {_rt_age:.0f}s），拦截 intent={_dispatch_intent}",
                          flush=True)
                    yield _sse("message", {
                        "text": "任务正在执行中，暂不支持当前操作，请稍候再试。",
                    })
                    yield _sse("done", {"phase": "processing"})
                    return
                elif _rt_state == "stale":
                    print(f"[chat] ⚠️ graph checkpoint 过旧（{_rt_age:.0f}s），"
                          f"可能已挂，尝试 astream(None) 恢复 intent={_dispatch_intent}",
                          flush=True)
                    _recovery_succeeded = False
                    try:
                        graph = _get_graph()
                        if graph is None:
                            raise RuntimeError("LangGraph 未初始化")
                        config = _langgraph_config(t_id)
                        # 服务重启后进程内 task 模型缓存已丢失；恢复 graph 前统一重建一次。
                        from wellflow.app.llm.model_pool import prepare_task_models
                        await prepare_task_models(t_id)
                        async for _chunk in graph.astream(None, config, stream_mode="updates"):
                            pass
                        print(f"[chat] ✅ astream(None) 续跑完成", flush=True)
                        _recovery_succeeded = True
                    except Exception as _exc:
                        print(f"[chat] ❌ astream(None) 恢复失败: {_exc}", flush=True)

                    if _recovery_succeeded:
                        # ------------------------------------------------------------------
                        # ✅ stale 恢复成功：graph 已从 checkpoint 续跑到下一个 interrupt。
                        #    此时绝对不能再走 dispatch invoke —— 那会把用户的"确认/选方案"
                        #    决策给跳过，直接把 graph 推到下一个 node，等于替用户做了决定。
                        #
                        #    正确做法：从 checkpoint state 读出 nodeX 的完整产物（schemes / prompts /
                        #    outputs），手动 yield 到 SSE 让前端在当前连接里直接看到卡片，
                        #    然后 yield done + return。用户看完后正常走 /api/tasks/{id}/resume 流程。
                        # ------------------------------------------------------------------
                        print(
                            f"[chat] ♻️ stale checkpoint 恢复成功 — "
                            f"graph 已续跑到下一个 interrupt",
                            flush=True,
                        )
                        # 从恢复后的 checkpoint 读 state
                        _, _new_state = await _aget_snapshot(t_id)
                        if not _new_state:
                            yield _sse("message", {"text": "检测到任务执行中断，已自动恢复到最新进度。"})
                            yield _sse("done", {"phase": "done"})
                            return
                        _node1 = _new_state.get("node1") or {}
                        _node2 = _new_state.get("node2") or {}
                        _node3 = _new_state.get("node3") or {}
                        _node4 = _new_state.get("node4") or {}
                        _ctx_after = resolve_current_node(
                            snapshot=None,
                            state=_new_state,
                        )
                        _current_after = _ctx_after.current_node  # "c1"/"c2"/"c3"/"c4"
                        _phase_map = {"c1": "c1_confirm", "c2": "c2_select",
                                      "c3": "c3_confirm", "c4": "c4_review"}
                        _phase = _phase_map.get(_current_after, "done")

                        # 合成 interrupt payload（复用 tasks.py 的模式）
                        _hint_map = {"c1": "请查看商品分析报告", "c2": "请选择商拍方案",
                                     "c3": "请确认生图提示词", "c4": ""}
                        _interrupt: dict[str, Any] = {"node": _current_after, "hint": _hint_map.get(_current_after, "")}
                        if _current_after == "c1":
                            _interrupt.update({
                                "report_sections": _node1.get("report_sections"),
                                "product_insight": _node1.get("product_insight"),
                                "thinking_text": _node1.get("thinking_text"),
                            })
                        elif _current_after == "c2":
                            _interrupt.update({
                                "schemes": _node2.get("schemes"),
                                "selected_scheme_indices": _node2.get("selected_scheme_indices"),
                                "scheme_raw": _node2.get("scheme_raw"),
                                "thinking_text": _node2.get("thinking_text"),
                            })
                        elif _current_after == "c3":
                            _interrupt.update({
                                "generate_prompts": _node3.get("generate_prompts"),
                                "prompts_detail": _node3.get("prompts_detail"),
                                "thinking_text": _node3.get("thinking_text"),
                            })
                        elif _current_after == "c4":
                            _interrupt.update({
                                "outputs": _node4.get("outputs"),
                                "failed_items": _node4.get("failed_items"),
                                "thinking_text": _node4.get("thinking_text"),
                            })
                        print(f"[chat] 📤 stale 恢复后 SSE payload node={_current_after}", flush=True)

                        # 1) phase 事件告诉前端当前阶段
                        yield _sse("phase", {"phase": _phase})
                        # 2) 消息事件带完整 interrupt payload —— 前端 interpretEvent 能直接识别为 workflowStep 卡片
                        _msg_payload: dict[str, Any] = {
                            "text": (
                                f"检测到任务执行中断，已自动恢复。请查看下方的{_hint_map.get(_current_after, '新内容')}，"
                                f"确认后再继续。"
                            ),
                            "interrupt": _interrupt,
                        }
                        if _current_after == "c2" and _interrupt.get("schemes"):
                            _msg_payload["schemes"] = _interrupt["schemes"]
                        elif _current_after == "c3" and _interrupt.get("generate_prompts"):
                            _msg_payload["generate_prompts"] = _interrupt["generate_prompts"]
                            if _interrupt.get("prompts_detail"):
                                _msg_payload["prompts_detail"] = _interrupt["prompts_detail"]
                        elif _current_after == "c4" and _interrupt.get("outputs"):
                            _msg_payload["outputs"] = _interrupt["outputs"]
                        elif _current_after == "c1":
                            _msg_payload["product_insight"] = _interrupt.get("product_insight")
                        _chunk = _sse("message", _msg_payload)
                        await _persist_sse_text(_chunk, known_task_id=t_id)
                        yield _chunk
                        # 3) done 告诉前端流结束
                        yield _sse("done", {"phase": _phase})
                        return
                    else:
                        yield _sse("message", {
                            "text": "检测到任务执行中断，但自动恢复失败。请重开窗口启动新任务。",
                        })
                        yield _sse("done", {"phase": "failed"})
                        return
                # paused → 放行

            # ------------------------------------------------------------------
            # dispatch：互斥分支
            # ------------------------------------------------------------------
            if _dispatch_intent == "start_task":
                async for ev in _pipe(_handle_start_task(
                    message, product_images, platform, image_type, marketing_goal, graph,
                    conversation_id=conv_id_for_this_turn,
                )):
                    yield ev

            elif _dispatch_intent == "edit":
                # ------------------------------------------------------------------
                # LLM 意图分类器返回 intent="edit" + refine_target=nodeX →
                # 转成 backward_to_<target> 调用 _handle_backward 走 refine 分支。
                # node1/2/3 是增量 refine；node4 是 redo（生图 API 无法增量编辑）。
                # ------------------------------------------------------------------
                _target = intent_result.get("refine_target")
                if not _target and current_node:
                    # LLM 没给 refine_target → 用当前 cX 对应 node 兜底
                    _FALLBACK_NODE = {"c1": "node1", "c2": "node2",
                                      "c3": "node3", "c4": "node4"}
                    _target = _FALLBACK_NODE.get(current_node, "node2")
                    print(f"[chat] ⚠️ LLM 未给 refine_target，兜底 → {_target}", flush=True)
                if not _target:
                    _target = "node2"
                _bd_intent = f"backward_to_{_target}"
                _instruction = intent_result.get("refine_instruction") or message

                # 🛑 拦截：本轮 refine 指令 == graph_state._refine_history[-1]（上一轮完全重复）
                # 但必须同时满足"上一轮 refine 成功产出了有效数据"——如果上一轮 refine
                # 因为 bug/异常没成功（产物为空或被清掉），就不能 block，必须允许重新执行
                if _instruction and _instruction.strip() and isinstance(graph_state, dict):
                    from wellflow.app.workflows.state import get_node_refine_history
                    # 🔴 重复检测按 node 隔离 —— 只看 _target（即将 refine 的那个 node）自己的历史
                    _history = get_node_refine_history(graph_state, _target)
                    print(f"[chat] 🔍 重复检测(target={_target}): instruction={_instruction[:60]} | per-node_history={_history}", flush=True)
                    if _history and normalize_instruction(_instruction) == normalize_instruction(_history[-1]):
                        if not _is_last_refine_succeeded(graph_state, _target):
                            # 指令重复，但上一轮 refine 没成功（产物为空）→ 放行让它重新跑
                            print(f"[chat] 🔄 指令重复但上一轮 refine 未成功（target={_target} 产物为空），放行", flush=True)
                        else:
                            print(f"[chat] 🛑 refine 指令与上一轮完全重复 → 拦截, instruction={_instruction}", flush=True)
                            _target_cn = {"node1": "报告", "node2": "方案", "node3": "提示词", "node4": "生图"}.get(_target, "产物")
                            _phase_cn = {"node1": "c1_confirm", "node2": "c2_select",
                                         "node3": "c3_confirm", "node4": "c4_review"}.get(_target, "done")
                            _lock_text = (
                                f"这条微调指令和上一轮完全一样哦，上一轮已经对{_target_cn}做过相同的修改了。"
                                "如果想继续调整，可以换一条不一样的指令～"
                            )
                            _chunk = _sse("message", {"text": _lock_text})
                            await _persist_sse_text(_chunk, known_task_id=t_id)
                            yield _chunk
                            yield _sse("done", {"phase": _phase_cn})
                            return
                    elif not _history:
                        print(f"[chat] → 未命中拦截：_refine_history 为空（上一轮 refine 未写入 history 或已被消费清空）", flush=True)
                    else:
                        print(f"[chat] → 未命中拦截：normalize 不相等 "
                              f"('{normalize_instruction(_instruction)}' vs '{normalize_instruction(_history[-1])}')", flush=True)

                print(f"[chat] edit → backward_to_{_target}, instruction={_instruction}",
                      flush=True)

                # LLM 意图分类器返回的 selected_indices（仅 node2 refine 有意义）
                # edit+refine_target=node2 时，LLM 会填它决定"哪几套方案要喂给 refine LLM"
                # 其他场景一律为 None → _handle_backward 会忽略
                _sel_indices = intent_result.get("selected_indices") if _target == "node2" else None

                async for ev in _pipe(_handle_backward(
                    _bd_intent, t_id or '', graph,
                    product_images=product_images,
                    refine_instruction=_instruction,
                    refine_selected_indices=_sel_indices,
                )):
                    yield ev

            else:
                async for ev in _pipe(_handle_resume(
                    _dispatch_intent, t_id or '', current_node, intent_result,
                    message, ref_images, product_images,
                    existing_report, existing_schemes,
                    existing_ref_images, graph,
                    selected_scheme_indices=selected_scheme_indices,
                    scheme_prompt_counts=scheme_prompt_counts,
                    reference_images_json=reference_images,
                    graph_state=graph_state,
                )):
                    yield ev
        except Exception as exc:
            import traceback as _tb2
            print(f"[chat] ❌ dispatch error intent={_dispatch_intent}: {exc}", flush=True)
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

    sku_paths: list[str] = []
    if not product_images and conversation_id:
        from wellflow.app.models.asset_models import ProductImage
        from sqlalchemy import select
        with session_scope() as db:
            conv = ConversationRepo(db).get(conversation_id)
            if conv and conv.sku_id:
                sku_paths = list(db.scalars(select(ProductImage.storage_uri).where(
                    ProductImage.sku_id == conv.sku_id, ProductImage.image_type == "product",
                ).order_by(ProductImage.sort_order, ProductImage.id)))
    if not product_images and not sku_paths:
        yield _sse("message", {"text": "该商品还没有商品图，请上传至少 1 张商品图片再开始。"})
        yield _sse("done", {"phase": "done"})
        return

    task_id = _short_uuid()
    q = await drain_and_subscribe(task_id)

    raw_files = [(f.filename or "image", await f.read(), f.content_type) for f in product_images]
    product_image_paths = save_upload(task_id, raw_files, prefix="p") if raw_files else sku_paths
    image_names = [f.filename for f in product_images] if raw_files else [p.rsplit("/", 1)[-1] for p in sku_paths]

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
        "image_count": len(product_image_paths),
        "has_images": bool(product_image_paths),
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
    refine_instruction: str | None = None,
    refine_selected_indices: list[int] | str | None = None,
) -> AsyncGenerator[str, None]:
    from wellflow.app.utils.image_store import save_upload

    # ------------------------------------------------------------------
    # 意图 → 回退目标映射
    # 这是 LLM 意图分类器 edit 意图的最终落点：dispatch 层把 edit + refine_target=nodeX
    # 转成 backward_to_nodeX 后交给 _handle_backward 执行。
    #   backward_to_node1 → refine node1（纯 text LLM 增量编辑报告）
    #   backward_to_node2 → refine node2（纯 text LLM 增量编辑商拍方案）
    #   backward_to_node3 → refine node3（纯 text LLM 增量编辑提示词）
    #   backward_to_node4 → redo node4（生图 API 无法增量编辑，保留完全重做）
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
    # node1/2/3 → refine；node4 → redo
    _IS_REFINE = redo_target in ("node1", "node2", "node3")

    # ------------------------------------------------------------------
    # Command 组装前置注释：
    #   phase == done → edit 意图（node4 redo 或 node1-3 refine）统一允许从 END 续跑
    #   LangGraph 的 Command(goto=X) 在 END checkpoint 上会从目标节点继续跑，
    #   state 保留 checkpoint 里已有的 node1/2/3/4 产物。
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

    # refine_instruction：优先用传入的显式参数，否则用用户原始 chat 消息做兜底
    _instruction = (refine_instruction or "").strip()
    if _IS_REFINE and not _instruction:
        print(f"[backward] ⚠️ refine 目标 {redo_target} 无指令，降级为通用 '重新生成' 指令", flush=True)
        _instruction = "请基于现有内容重新生成一份，保持整体风格不变。"

    # 注意：node1 锁定守卫已在 chat.py 顶部统一处理（API 层），
    # _handle_backward 里不再重复拦截——如果 intent 已走到这里，说明 API 层已放行。

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
        # ── 路径 B：graph 已 END ──
        # node1/2/3 refine → Command(goto=目标 refine 节点) + 写 _refine_target/_refine_instruction
        # node4 redo → 保持原有清理逻辑 + goto node4_generate_image
        _, latest_state = await _aget_snapshot(task_id)
        latest_state = latest_state or {}

        from wellflow.app.workflows.state import cleared
        node1 = latest_state.get("node1", {}) or {}
        node2 = latest_state.get("node2", {}) or {}
        node3 = latest_state.get("node3", {}) or {}
        node4 = latest_state.get("node4", {}) or {}

        if _IS_REFINE:
            # refine 目标 → 不做 state 清理（refine 是原地增量编辑，保留所有上下游产物）
            refine_node_map = {
                "node1": "node1_refine_report",
                "node2": "node2_refine_schemes",
                "node3": "node3_refine_prompts",
            }
            exec_node = refine_node_map.get(redo_target)
            if not exec_node:
                yield _sse("message", {"text": f"不支持的 refine 目标: {redo_target}"})
                yield _sse("done", {"phase": "done"})
                return
            from wellflow.app.workflows.state import build_refine_history_update
            _prev_history = latest_state.get("_refine_history")
            refine_update = {
                "_refine_target": redo_target,
                "_refine_instruction": _instruction,
                "_refine_selected_indices": refine_selected_indices,  # None for non-node2 refine → refine_node2_schemes 会兜底传全部
                # 🔴 历史按 node 隔离写入（只写入 redo_target 对应的 list，其他 node 的历史通过全局 dict merge 保留）
                "_refine_history": build_refine_history_update(_prev_history, redo_target, _instruction or ""),
                "_redo_target": None,
            }
            update_dict = {**refine_update, **cmd_update} if cmd_update else refine_update
            cmd = Command(goto=exec_node, update=update_dict)
            print(f"[backward] 🎯 graph已END → refine→{redo_target} goto={exec_node}, instruction={_instruction}", flush=True)
        else:
            # redo node4：无论上一轮是否失败，都重新生成全部图片
            redo_target_cleanup: dict[str, dict[str, Any]] = {}
            from wellflow.app.workflows.node4_graph import prepare_image_redo
            new_node4 = prepare_image_redo(node4)
            redo_target_cleanup = {"node4": new_node4, "phase": "c4_review"}
            update_dict = {**redo_target_cleanup, **cmd_update} if cmd_update else redo_target_cleanup
            exec_node = _EXEC_NODE_OF_TARGET.get(redo_target, redo_target)
            cmd = Command(goto=exec_node, update=update_dict)
            print(f"[backward] 🎯 graph已END → redo node4 goto={exec_node}", flush=True)
    else:
        # ── 路径 A：graph 停在 cX interrupt → Command(resume=...) ──
        if _IS_REFINE:
            # refine：让 _cX_confirm / _c4_review 的 refine 分支消费
            resume_values = {
                "node": resume_node,
                "decision": "refine",
                "refine_target": redo_target,
                "refine_instruction": _instruction,
                "refine_selected_indices": refine_selected_indices,  # None for non-node2 refine → 下游忽略
            }
        else:
            # redo node4
            resume_values = {
                "node": resume_node,
                "decision": "redo",
                "redo_target": redo_target,
            }
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
    ref_images: dict[str, list[UploadFile]],
    product_images: list[UploadFile] | None,
    existing_report: str,
    existing_schemes: list,
    existing_ref_images: dict[str, list[str]],
    graph,
    *,
    selected_scheme_indices: str | None = None,
    scheme_prompt_counts: str | None = None,
    reference_images_json: str | None = None,
    graph_state: dict[str, Any] | None = None,
) -> AsyncGenerator[str, None]:
    from wellflow.app.utils.image_store import save_upload

    if not current_node:
        yield _sse("message", {"text": "当前没有可继续的节点，请先开始一个任务。"})
        yield _sse("done", {"phase": "done"})
        return

    # 注意：validate_current_node_products 硬性守卫已按要求移除
    # （用户要求除 node1 锁定 redo block 外，其他 block 全部移除）

    q = await drain_and_subscribe(task_id)

    # —— 三类参考图落盘 ——
    ref_paths: dict[str, list[str]] = {"mannequin": [], "scene": [], "outfit": []}
    for k in ("mannequin", "scene", "outfit"):
        files = ref_images.get(k) or []
        if files:
            raw = [(f.filename or k, await f.read(), f.content_type) for f in files]
            ref_paths[k] = save_upload(task_id, raw, prefix=k[0])

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
        _current_report_hash = _quick_report_hash(existing_report)

        # ── edit_and_confirm_c1：自然语言补充/修改报告 → 走 refine 路径（与 c2/c3 一致）──
        if intent == "edit_and_confirm_c1":
            resume_values["decision"] = "refine"
            resume_values["refine_target"] = "node1"
            resume_values["refine_instruction"] = message
            print(f"[chat] c1 edit_and_confirm_c1 → refine 路径, instruction={message}", flush=True)
        else:
            resume_values["confirmed_report"] = existing_report
            # 🔴 版本绑定：把当前报告的 hash 原样带回，_c1_confirm_report 会校验
            resume_values["report_hash"] = _current_report_hash
        # C1 允许传 mannequin 图（前端自动补）
        if ref_paths.get("mannequin"):
            resume_values["reference_images"] = {"mannequin": ref_paths["mannequin"], "scene": [], "outfit": []}
        resume_values.setdefault("ratio", "9:16竖版")
        resume_values.setdefault("count", 3)

    elif node == "c2":
        # ── edit_and_confirm_c2：用户想微调/修改商拍方案 → 走 refine 路径 ──
        if intent == "edit_and_confirm_c2":
            resume_values["decision"] = "refine"
            resume_values["refine_target"] = "node2"
            resume_values["refine_instruction"] = message
            print(f"[chat] c2 edit_and_confirm_c2 → refine 路径, instruction={message}", flush=True)
        else:
            # confirm_current / confirm_generation / chat_outside 等非编辑类意图
            #
            # 选中项优先级：
            #   1) 前端 checkbox 显式传的 selected_scheme_indices（resume 表单）
            #   2) 意图分类器从消息文本解析的 intent_result.selected_indices（"选第1套"/"全选"）
            #
            # 多套方案必须明确选中一套；微调/融合后仅剩一套时，
            # "继续" 可以直接确认唯一的最终方案。
            #    卡片确认走 /api/tasks/{id}/resume，输入框确认走这里。
            indices: list[int] = []
            if selected_scheme_indices:
                for part in selected_scheme_indices.split(","):
                    part = part.strip()
                    if part.isdigit():
                        indices.append(int(part))
            if not indices:
                # 自然语言中的“方案1”是用户可见的第 1 套，内部索引为 0。
                explicit = re.search(r"方案\s*([1-9]\d*)|第\s*([1-9]\d*)\s*套", message)
                if explicit:
                    idx = int(explicit.group(1) or explicit.group(2)) - 1
                    if 0 <= idx < len(existing_schemes):
                        indices = [idx]
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
            if not indices and len(existing_schemes) == 1:
                indices = [0]
            if len(indices) != 1:
                print(
                    f"[chat] 🛑 c2 阶段必须明确选中一套方案 → 拦截, "
                    f"existing_schemes={len(existing_schemes)}",
                    flush=True,
                )
                yield _sse("message", {
                    "text": "请先查看商拍方案卡片，选 1 套您满意的方案后再点击确认。每套方案风格不同，最终只保留 1 套进入提示词生成。",
                })
                yield _sse("done", {"phase": "c2_select"})
                return
            resume_values["selected_scheme_indices"] = indices
            try:
                all_counts = json_mod.loads(scheme_prompt_counts or "[]")
                counts = [all_counts[idx] for idx in indices]
                if any(type(count) is not int or count < 1 for count in counts):
                    raise ValueError("无效数量")
            except (ValueError, TypeError, IndexError):
                yield _sse("message", {"text": "未收到所选方案的提示词数量，请在方案卡片中确认。"})
                yield _sse("done", {"phase": "c2_select"})
                return
            resume_values["per_scheme_count"] = counts
            print(f"[chat] c2 输入框确认 selected={indices} per_scheme_count={counts}", flush=True)
        # C2 阶段三类参考图全注入
        supplied_refs: dict[str, Any] = {}
        if reference_images_json:
            try:
                parsed_refs = json_mod.loads(reference_images_json)
                if isinstance(parsed_refs, dict):
                    supplied_refs = parsed_refs
            except (ValueError, TypeError):
                pass
        references_for_resume: dict[str, list[str]] = {}
        for kind in ("mannequin", "scene", "outfit"):
            submitted = supplied_refs.get(kind)
            saved = existing_ref_images.get(kind)
            values = submitted if isinstance(submitted, list) else saved
            references_for_resume[kind] = [
                uri for uri in (values or []) if isinstance(uri, str) and uri
            ] + list(ref_paths.get(kind) or [])
        resume_values["reference_images"] = references_for_resume

    elif node == "c3":
        # ── edit_and_confirm_c3：用户想微调/修改提示词 → 走 refine 路径 ──
        if intent == "edit_and_confirm_c3":
            resume_values["decision"] = "refine"
            resume_values["refine_target"] = "node3"  # c3 下默认改 node3 提示词，也可改 node2 方案
            resume_values["refine_instruction"] = message
            print(f"[chat] c3 edit_and_confirm_c3 → refine 路径, instruction={message}", flush=True)
        else:
            # C3 下的 confirm_current / select_topics 等非编辑意图：
            # 解析 intent_result.selected_indices → 映射成 selected_prompt_indices 传给 LangGraph。
            # 优先级和 c2 阶段一致：
            #   1) intent_result.selected_indices（意图分类器从自然语言里解析的）
            #      例："采用第一个提示词" → ["0"]；"全部" → "all"
            #   2) 都没有 → 不设置（_c3_confirm_prompt 不做过滤，默认全部往下跑）
            _c3_prompts = (graph_state or {}).get("node3", {}).get("generate_prompts") or []
            _c3_prompt_total = len(_c3_prompts)
            selected = intent_result.get("selected_indices") if intent_result else None
            if _c3_prompt_total and selected is not None:
                if selected == "all":
                    resume_values["selected_prompt_indices"] = list(range(_c3_prompt_total))
                    print(f"[chat] c3 selected_indices='all' → 全部 {_c3_prompt_total} 条", flush=True)
                elif isinstance(selected, list):
                    indices: list[int] = []
                    for x in selected:
                        try:
                            idx = int(x)
                            if 0 <= idx < _c3_prompt_total:
                                indices.append(idx)
                        except (ValueError, TypeError):
                            pass
                    if indices:
                        resume_values["selected_prompt_indices"] = sorted(set(indices))
                        print(f"[chat] c3 selected_indices={selected} → selected_prompt_indices={indices}", flush=True)

            # 若意图分类器把 c3 纯选择判成了 select_topics（新增意图），
            # selected_prompt_indices 已在上面填好；confirm_current 则默认全选。
            if any(ref_paths.values()):
                resume_values["reference_images"] = {
                    "mannequin": ref_paths.get("mannequin") or [],
                    "scene": ref_paths.get("scene") or [],
                    "outfit": ref_paths.get("outfit") or [],
                }

            # 给用户一条可读的选择确认 —— 前端会直接展示这条 message 事件
            _spi = resume_values.get("selected_prompt_indices")
            if _spi:
                if len(_spi) == _c3_prompt_total:
                    _pick_msg = f"好的，采用全部 {_c3_prompt_total} 条提示词开始生图…"
                elif len(_spi) == 1:
                    _pick_msg = f"好的，采用第 {_spi[0] + 1} 条提示词开始生图…"
                else:
                    _pick_msg = (
                        f"好的，采用第 "
                        + "、".join(str(i + 1) for i in _spi)
                        + f" 条提示词（共 {len(_spi)} 条）开始生图…"
                    )
                _chunk = _sse("message", {"text": _pick_msg})
                yield _chunk

    elif node == "c4":
        if intent != "redo_generation":
            yield _sse("message", {"text": "请在图片结果中勾选需要的图片，点击入库保存到关联 SKU。"})
            from wellflow.app.services.sku_archive import with_image_keys
            payload = with_image_keys((graph_state or {}).get("node4") or {})
            yield _sse("interrupt", {**payload, "node": "c4", "phase": "c4_review", "hint": "请选择图片后入库"})
            return
        # C4 独占 confirm_generation / redo_generation 两个意图。
        # 统一走 decision + redo_target（graph 的 _c4_review_result 读这两个字段）。
        if intent == "redo_generation":
            resume_values["decision"] = "redo"
            resume_values["redo_target"] = "node4"  # 默认只重生图，保留上游 node1/2/3
        else:
            # confirm_generation（c4 下所有非 redo 的意图都当作确认）
            resume_values["decision"] = "confirm"

    print(f"[chat] resume_values node={node}: {list(resume_values.keys())}", flush=True)

    if node in ("c1", "c2", "c3") and resume_values.get("reference_images"):
        def _save_references():
            with session_scope() as db:
                TaskRepo(db).save_reference_images(task_id, resume_values["reference_images"])
        await asyncio.to_thread(_save_references)

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
