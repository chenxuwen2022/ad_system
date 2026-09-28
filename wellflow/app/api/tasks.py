"""任务相关 REST API —— 优化版。

核心优化：
  1. create_task / resume_task 直接返回 StreamingResponse（SSE），一个 HTTP 搞定，
     前端不再需要两段式（POST → 等 task_id → 再连 SSE）
  2. 同步 DB 写（repo.create / repo.add_event）改为 asyncio.to_thread fire-and-forget，
     不阻塞 HTTP handler 提前返回给前端
  3. 第一个 SSE event 就是 task_id，前端立即拿到
  4. graph.astream 的 chunk 通过 event_bus.publish → SSE queue → StreamingResponse 推给前端
"""

from __future__ import annotations

from wellflow.app.logging import log_message, page_context

from wellflow.app.workflow_status import canonical_phase

import asyncio
import json as json_mod
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, Request
from fastapi.responses import StreamingResponse
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from sqlalchemy.orm import Session

from wellflow.app.database import get_db, session_scope
from wellflow.app.workflow_execution import launch_graph
from wellflow.app.api.utils import ok, StandardResponse, to_cn_iso
from wellflow.app.config import settings
from wellflow.app.graph_persist import persist_phase, persist_interrupt, persist_error, persist_outputs
from wellflow.app.models.task_models import TaskPhase
from wellflow.app.repositories.task_repo import TaskRepo
from wellflow.app.repositories.conversation_repo import _compose_title_from_hint
from wellflow.app.schemas.task_schemas import (
    TaskListResponse,
    TaskListItem,
    TaskInfoResponse,
)


router = APIRouter(prefix="/tasks", tags=["任务"])


def _short_uuid() -> str:
    return uuid.uuid4().hex[:12]


def _get_graph():
    from wellflow.app.runtime import get_graph
    g = get_graph()
    if g is None:
        raise RuntimeError("LangGraph 未初始化")
    return g


def _langgraph_config(task_id: str) -> RunnableConfig:
    return RunnableConfig(configurable={"thread_id": task_id})


def _extract_interrupt_value(chunk: Any) -> dict[str, Any] | None:
    """从 ('updates', {'__interrupt__': (Interrupt(...),)}) 里提取 interrupt value。"""
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


# ---------------------------------------------------------------------------
# graph chunk 处理（推 SSE + 按流顺序写 DB）
# ---------------------------------------------------------------------------



def _storage_uri_url(storage_uri: str) -> str:
    """相对路径 → 可被前端直接访问的 URL（静态挂载 /uploads 提供）。"""
    if not storage_uri:
        return ""
    if storage_uri.startswith("http"):
        return storage_uri
    return "/" + storage_uri.lstrip("/")


def _sse(event: str, data: dict[str, Any]) -> str:
    """构造一个 SSE 事件字符串。"""
    return f"event: {event}\ndata: {json_mod.dumps(data, ensure_ascii=False)}\n\n"


# ===========================================================================
# create_task —— 现在返回 StreamingResponse（SSE），一个 HTTP 搞定
# ===========================================================================


@router.post("", summary="创建商拍任务（SSE 直推，零延迟）")
@page_context('对话')
async def create_task(
    request: Request,
    description: str = Form(default=""),
    platform: str = Form(default="taobao"),
    image_type: str = Form(default="ad"),
    marketing_goal: str = Form(default="acquisition"),
    product_link: str | None = Form(default=None),
    image_model: str | None = Form(default=None),
    brand_config: str | None = Form(default=None),
    product_images: list[UploadFile] = File(default_factory=list),
):
    """创建商拍任务（multipart/form-data）。返回 SSE 流，第一个 event 就是 task_id。

    相比旧版优化：前端只需要一个 POST 请求，不再需要先等 JSON 响应再连 SSE。
    """
    if not product_images:
        raise HTTPException(status_code=400, detail="product_images 为必传参数，请至少上传一张商品图片")

    t0 = time.time()
    task_id = _short_uuid()
    # 为这次按钮直跑创建独立的 conversation —— 让它也有自己的侧边栏条目
    conversation_id = _short_uuid()
    log_message(f"[create_task] 🚀 开始, task_id={task_id} conversation_id={conversation_id}", page='对话', business='创建商拍任务', status='记录')

    # --- Step 1: 读上传文件 → 落盘（同步 I/O，放 to_thread 里更快？不，FastAPI UploadFile.read 是 async 的） ---
    raw_files = [(f.filename or "image", await f.read(), f.content_type) for f in product_images]
    from wellflow.app.utils.image_store import save_upload
    product_image_paths = save_upload(task_id, raw_files, prefix="p")
    log_message(f"[create_task] 📁 图片落盘完成 ({time.time() - t0:.2f}s), paths={len(product_image_paths)}", page='对话', business='创建商拍任务', status='记录')

    image_names = [f.filename for f in product_images]
    # 生成简短 description 供前端历史列表展示
    _desc = description.strip()[:30]
    if not _desc:
        _desc = " ".join(n.rsplit(".", 1)[0] for n in image_names[:2]) or "新商拍任务"
    request_json: dict[str, Any] = {
        "description": _desc,
        "platform": platform,
        "image_type": image_type,
        "marketing_goal": marketing_goal,
        "product_link": product_link,
        "image_model": image_model,
        "image_count": len(product_images),
        "has_images": len(product_images) > 0,
        "has_text": bool(description),
        "product_image_names": image_names,
        "product_images": product_image_paths,
    }
    brand_cfg: dict[str, Any] = {}
    if brand_config:
        try:
            brand_cfg = json_mod.loads(brand_config)
        except json_mod.JSONDecodeError:
            raise HTTPException(400, "brand_config 必须是合法 JSON 字符串")

    # --- Step 2: 注册 event_bus queue（先注册，再 fire-and-forget 后面的事） ---
    from wellflow.app.event_bus import drain_and_subscribe
    q = await drain_and_subscribe(task_id)

    # --- Step 3: fire-and-forget 启动 graph + DB 写入（不阻塞 HTTP handler） ---
    try:
        graph = _get_graph()
        config = _langgraph_config(task_id)
        initial_state: dict[str, Any] = {
            "task_id": task_id,
            "phase": TaskPhase.INPUT.value,
            "request": request_json,
            "brand_config": brand_cfg,
            "node1": {}, "node2": {}, "node3": {}, "node4": {},
            "selected_plan_ids": [],
            "progress": {},
            "cost": {},
            "interrupt": None,
            "error": None,
            "event_ids": [],
        }

        # fire-and-forget 写 DB + 启动 graph
        # title_hint 让 conversation 在建的时候就有真实 title（不是"新对话"占位）
        _title_hint = {
            "kind": "button_create_task",
            "description": description,
            "product_link": product_link,
            "image_filenames": [f.filename or "" for f in product_images],
            "platform": platform,
        }
        _computed_conv_title = _compose_title_from_hint(_title_hint)
        asyncio.create_task(_create_task_in_background(
            task_id, conversation_id, _title_hint,
            request_json, brand_cfg, graph, config, initial_state,
        ))
    except Exception as exc:
        raise HTTPException(503, "工作流暂不可用，请稍后重试") from exc

    # --- Step 4: 返回 StreamingResponse ---
    # 第一个 event 是 task_created（含 task_id），然后从 queue 读事件推给前端
    async def event_generator():
        from wellflow.app.event_bus import publish
        # 首 event：task_id + 元信息
        yield _sse("task_created", {
            "task_id": task_id,
            "phase": TaskPhase.INPUT.value,
            "estimated_cost_range": [2.0, 10.0],
            "description": _desc,
        })

        # conversation title —— 提前算好直接发，前端侧边栏无需刷新就能显示
        yield _sse("conversation_title", {
            "conversation_id": conversation_id,
            "title": _computed_conv_title,
        })
        log_message(f"[create_task] 📢 conversation_title={_computed_conv_title}", page='对话', business='任务管理', status='记录')

        # 先推一个 phase=input（对齐旧版 SSE 契约）
        yield _sse("phase", {"phase": "input"})

        # 主循环：从 queue 读事件推给前端
        last_event_ts = time.time()
        while True:
            if await request.is_disconnected():
                log_message(f"[sse] task={task_id} 前端断开连接", page='对话', business='任务管理', status='记录')
                break

            # 带超时的 get，用于 heartbeat
            try:
                event = await asyncio.wait_for(q.get(), timeout=settings.sse_heartbeat_interval_seconds)
                last_event_ts = time.time()
                event_type = event.get("type")
                event_data = event.get("data", {})
                log_message(f"[sse] task={task_id} ← queue got type={event_type}", page='对话', business='任务管理', status='记录')

                # 处理各类事件
                yield _handle_sse_event(event_type, event_data)

                # 终态：done / error → 结束 SSE 流
                if event_type == "done" or (event_type == "phase" and event_data.get("phase") == "failed"):
                    log_message(f"[sse] task={task_id} 终态到达，关闭 SSE", page='对话', business='任务管理', status='记录')
                    break
                if event_type == "error":
                    log_message(f"[sse] task={task_id} error，关闭 SSE", page='对话', business='任务管理', status='记录')
                    break
                # interrupt 表示 graph 停在 HITL 等待用户输入，这一轮 SSE 应该关闭
                # 前端下一次 resume 会发新的 HTTP 请求开启新 SSE 流
                if event_type == "interrupt":
                    log_message(f"[sse] task={task_id} interrupt(node={event_data.get('node')})，关闭 SSE（等待用户确认）", page='对话', business='任务管理', status='记录')
                    break

            except asyncio.TimeoutError:
                # heartbeat
                elapsed = time.time() - last_event_ts
                if elapsed >= settings.sse_heartbeat_interval_seconds:
                    yield ": heartbeat\n\n"
                    last_event_ts = time.time()

    log_message(f"[create_task] ✅ 耗时 {time.time() - t0:.2f}s，返回 StreamingResponse", page='对话', business='创建商拍任务', status='成功')
    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def _create_task_in_background(
    task_id: str,
    conversation_id: str,
    title_hint: dict[str, Any],
    request_json: dict[str, Any],
    brand_cfg: dict[str, Any],
    graph,
    config,
    initial_state: dict[str, Any],
):
    """后台任务：先写 DB（必须成功），再启动 graph。

    DB create 必须在 graph 启动前完成——否则 graph 很快跑到 interrupt 点，
    前端立即调 resume，此时 Task 行还没写入就会 404。
    conversation 必须先于 task 创建（FK 约束）。
    """
    def _sync_write():
        with session_scope() as db:
            from wellflow.app.repositories.conversation_repo import ConversationRepo
            conv_repo = ConversationRepo(db)
            # conversation —— 带 title_hint，repo 会自动生成真实 title
            conv_repo.create(
                conversation_id=conversation_id,
                title="新对话",
                current_task_id=task_id,
                title_hint=title_hint,
            )
            repo = TaskRepo(db)
            repo.create(
                task_id=task_id,
                request_json=request_json,
                phase=TaskPhase.INPUT.value,
                brand_config_json=brand_cfg,
                conversation_id=conversation_id,
            )
            repo.add_event(task_id, "task_created", phase=TaskPhase.INPUT.value, payload_json=request_json)

    try:
        await asyncio.to_thread(_sync_write)
    except Exception as e:
        from wellflow.app.event_bus import publish as _eb
        log_message(f"[create_task] ❌ DB 写入失败: {e}", page='对话', business='任务管理', status='失败')
        _eb(task_id, "error", {"phase": "failed", "message": f"任务创建失败（DB）: {e}"})
        return  # 不启动 graph

    # DB 就绪，启动 graph
    try:
        await launch_graph(task_id, graph, config, initial_state=initial_state)
    except Exception as exc:
        from wellflow.app.event_bus import publish
        publish(task_id, "error", {"phase": "needs_retry", "message": str(exc)})


def _handle_sse_event(event_type: str, event_data: dict[str, Any]) -> str:
    """保留阶段事件的精简格式，其余已知事件直接透传。"""
    if event_type == "phase":
        return _sse("phase", {"phase": event_data.get("phase")})
    if event_type in {
        'done', 'error', 'interrupt',
        'node4_image_done', 'node4_image_failed', 'prompt_chunk',
        'prompt_chunk_done', 'report_chunk', 'report_chunk_done',
        'scheme_chunk', 'scheme_chunk_done', 'thinking_chunk',
    }:
        return _sse(event_type, event_data)
    return ""


# ===========================================================================
# resume_task —— 也改成返回 StreamingResponse
# ===========================================================================


@router.post("/{task_id}/resume", summary="从 HITL 点恢复任务（SSE 直推）")
@page_context('对话')
async def resume_task(
    task_id: str,
    request: Request,
    node: str = Form(...),
    confirmed_report: str = Form(default=""),
    ratio: str = Form(default="9:16"),
    # action: "confirm" | "refine" | "redo"(仅 C4 redo→node4 保留)
    action: str = Form(default="confirm"),
    image_model: str | None = Form(default=None),
    selected_scheme_indices: str | None = Form(default=None),
    # redo_target: C4 阶段可选，仅 redo 模式生效
    redo_target: str | None = Form(default=None),
    # refine 专用字段：C1/C2/C3/C4 都支持
    refine_instruction: str | None = Form(default=None),
    refine_target: str | None = Form(default=None),
    resume_json: str | None = Form(default=None),
    revision: str | None = Form(default=None),
    # 三类参考图（一步到位：直接在 resume 请求里把图传过来）
    mannequin_files: list[UploadFile] = File(default_factory=list),
    scene_files: list[UploadFile] = File(default_factory=list),
    outfit_files: list[UploadFile] = File(default_factory=list),
    db: Session = Depends(get_db),
):
    import json as _json

    repo = TaskRepo(db)
    task = repo.get(task_id)
    if not task:
        raise HTTPException(404, f"task {task_id} 不存在")

    if task.phase == "archive_pending":
        raise HTTPException(409, "图片入库待完成，请用原选择重试入库")

    # 终态守卫：确认入库（done）后禁止任何 redo / resume
    if task.phase == TaskPhase.DONE.value:
        raise HTTPException(409, "任务已确认入库，禁止任何重做操作")

    # 并发保护：graph 正在执行时禁止 resume（防止并发跑两个 graph 互相覆盖 interrupt_json）
    from wellflow.app.event_bus import is_running as _is_running
    if _is_running(task_id):
        raise HTTPException(409, "任务正在执行中，请等待完成后再操作")

    graph_state, snapshot = await _aget_graph_state(task_id)
    if graph_state is None:
        raise HTTPException(503, "无法读取工作流状态，请稍后重试")
    from wellflow.app.workflow_status import checkpoint_view
    _, interrupt = checkpoint_view(snapshot)
    if not interrupt:
        raise HTTPException(409, "任务没有暂停点，请恢复执行或刷新任务")
    current_node = interrupt["node"]
    if current_node != node:
        raise HTTPException(409, f"当前暂停在 {current_node}，请刷新后操作")

    # ------------------------------------------------------------------
    # 🛡️ C4 入库守卫：confirm 是不可逆的终态操作，必须显式声明意图。
    #    历史缺陷：action 缺失时落入 confirm 分支，导致静默入库。
    # ------------------------------------------------------------------
    if current_node == "c4":
        if resume_json:
            try:
                _rj_guard = _json.loads(resume_json)
                _has_explicit_decision = (
                    isinstance(_rj_guard, dict)
                    and _rj_guard.get("decision") in ("confirm", "redo", "refine")
                )
            except Exception:
                _has_explicit_decision = False
        else:
            _has_explicit_decision = action in ("confirm", "redo", "refine")
        if not _has_explicit_decision:
            raise HTTPException(
                400,
                "node=c4 的 resume 必须显式携带 decision（confirm/redo/refine），禁止默认确认入库",
            )

    # ------------------------------------------------------------------
    # 🛡️ 硬性规则校验：绝不允许跳过损坏 / 数据缺失的节点（仅 confirm 路径）
    #   refine 路径：就是因为产物可能有问题才想编辑，不应被产物完整性拦截
    #   redo 路径（仅 C4 redo→node4）：重置 work_items 也应绕过
    # ------------------------------------------------------------------
    _decision_for_validate = action
    # resume_json 可能覆盖 Form action（resume_json 优先级最高）
    if resume_json:
        try:
            _rj = _json.loads(resume_json)
            if isinstance(_rj, dict) and _rj.get("decision"):
                _decision_for_validate = _rj["decision"]
        except Exception:
            pass

    _skip_validate = (_decision_for_validate in ("refine", "redo"))

    if not _skip_validate:
        from wellflow.app.graph_context import validate_current_node_products
        ok, missing = validate_current_node_products(current_node, graph_state)
        if not ok:
            log_message(f"[resume] 🛡️ 硬性守卫拦截 current_node={current_node}，"
                f"缺失产物: {missing} → 拒绝 resume，请 retry 当前 node", page='对话', business='恢复商拍任务', status='记录')
            raise HTTPException(
                409,
                f"当前节点({current_node})产物缺失或损坏({missing})，"
                "请重试当前节点，不允许跳过向下流转。",
            )

    # ---- 三类参考图落盘（resume 时一步到位：直接从 FormData 上传）----
    ref_paths: dict[str, list[str]] = {"mannequin": [], "scene": [], "outfit": []}
    if mannequin_files or scene_files or outfit_files:
        from wellflow.app.utils.image_store import save_upload as _save
        _pairs = [
            ("mannequin", mannequin_files),
            ("scene", scene_files),
            ("outfit", outfit_files),
        ]
        for k, files in _pairs:
            if files:
                raw = [(f.filename or k, await f.read(), f.content_type) for f in files]
                ref_paths[k] = _save(task_id, raw, prefix=k[0])
        log_message(f"[resume] 📎 三类参考图落盘: "
              f"mannequin={len(ref_paths['mannequin'])}, scene={len(ref_paths['scene'])}, outfit={len(ref_paths['outfit'])}", page='对话', business='恢复商拍任务', status='记录')

    # ---- 优先用 resume_json（最灵活的通路） ----
    if resume_json:
        try:
            resume_values = _json.loads(resume_json)
            if not isinstance(resume_values, dict):
                raise ValueError("resume_json 必须是 JSON 对象")
        except Exception as exc:
            raise HTTPException(400, f"resume_json 解析失败: {exc}")
        # 注入通用字段（Form 参数仍可覆盖 json）
        resume_values.setdefault("node", node)
        # FormData 上传的三类图 → 注入（resume_json 里没写 reference_images 或某类为空时使用）
        if any(ref_paths.values()):
            _existing_refs = resume_values.get("reference_images") or {}
            resume_values["reference_images"] = {
                "mannequin": list(_existing_refs.get("mannequin") or ref_paths["mannequin"]),
                "scene": list(_existing_refs.get("scene") or ref_paths["scene"]),
                "outfit": list(_existing_refs.get("outfit") or ref_paths["outfit"]),
            }
    else:
        # ---- 按 node 类型组装 Form 参数 ----
        resume_values: dict[str, Any] = {"node": node}
        _is_refine = (action == "refine")
        _is_redo = (action == "redo")

        # ---- 通用 refine / redo 拦截：C1/C2/C3 不支持完全 redo ----
        if _is_redo and node in ("c1", "c2", "c3"):
            log_message(f"[resume] ⚠️ node={node} 不支持完全重做（redo），自动降级为 refine", page='对话', business='恢复商拍任务', status='警告')
            _is_redo = False
            _is_refine = True

        # refine 通用注入
        if _is_refine:
            if not refine_instruction:
                raise HTTPException(400, f"node={node} action=refine 时 refine_instruction 为必传参数")
            resume_values["decision"] = "refine"
            resume_values["refine_instruction"] = refine_instruction.strip()
            if refine_target:
                resume_values["refine_target"] = refine_target
            log_message(f"[resume] node={node} refine target={refine_target or 'default'} "
                  f"instruction={refine_instruction.strip()}", page='对话', business='恢复商拍任务', status='记录')

        # redo 通用注入（仅 C4 redo→node4 保留）
        if _is_redo:
            if node != "c4":
                raise HTTPException(400, f"node={node} 不支持完全重做（redo），请使用 action=refine")
            target = redo_target or "node4"
            if target != "node4":
                raise HTTPException(400, f"C4 redo 仅支持 redo_target=node4，node2/node3 请使用 action=refine")
            resume_values["decision"] = "redo"
            resume_values["redo_target"] = target
            if image_model is not None:
                resume_values["image_model"] = image_model
            log_message(f"[resume] C4 redo → {target}", page='对话', business='恢复商拍任务', status='记录')

        # ---- confirm 模式：按 node 分支处理正常流转 ----
        if not _is_refine and not _is_redo:
            if node == "c1":
                resume_values["confirmed_report"] = confirmed_report
                resume_values["ratio"] = ratio
                if image_model:
                    resume_values["image_model"] = image_model
                # C1 允许传 mannequin 图
                if ref_paths.get("mannequin"):
                    resume_values["reference_images"] = {
                        "mannequin": ref_paths["mannequin"], "scene": [], "outfit": []
                    }
            elif node == "c2":
                if image_model:
                    resume_values["image_model"] = image_model
                if ratio:
                    resume_values["ratio"] = ratio
                if selected_scheme_indices:
                    try:
                        indices = [int(x) for x in selected_scheme_indices.split(",") if x.strip()]
                        resume_values["selected_scheme_indices"] = indices
                        log_message(f"[resume] C2 收到 selected_scheme_indices={indices}", page='对话', business='恢复商拍任务', status='记录')
                    except ValueError:
                        log_message(f"[resume] ⚠️ selected_scheme_indices 解析失败: {selected_scheme_indices}", page='对话', business='恢复商拍任务', status='警告')
                _psc = (await request.form()).get("per_scheme_count")
                if _psc:
                    try:
                        counts = [int(x) for x in _psc.split(",") if x.strip()]
                        resume_values["per_scheme_count"] = [max(1, c) for c in counts]
                        log_message(f"[resume] C2 收到 per_scheme_count={counts}", page='对话', business='恢复商拍任务', status='记录')
                    except ValueError:
                        log_message(f"[resume] ⚠️ per_scheme_count 解析失败: {_psc}", page='对话', business='恢复商拍任务', status='警告')
                # C2 三类参考图全注入
                if any(ref_paths.values()):
                    resume_values["reference_images"] = {
                        "mannequin": ref_paths["mannequin"],
                        "scene": ref_paths["scene"],
                        "outfit": ref_paths["outfit"],
                    }
            elif node == "c3":
                if image_model:
                    resume_values["image_model"] = image_model
                if ratio:
                    resume_values["ratio"] = ratio
                if any(ref_paths.values()):
                    resume_values["reference_images"] = {
                        "mannequin": ref_paths["mannequin"],
                        "scene": ref_paths["scene"],
                        "outfit": ref_paths["outfit"],
                    }
            elif node == "c4":
                resume_values["decision"] = "confirm"

    supplied_revision = resume_values.get("revision") or revision
    if supplied_revision and supplied_revision != interrupt.get("revision"):
        raise HTTPException(409, "内容已更新，请刷新后重新确认")
    resume_values["revision"] = interrupt.get("revision")
    resume_values["node"] = current_node

    if node == "c4" and resume_values.get("decision", "confirm") == "confirm":
        raise HTTPException(409, "请在图片结果中勾选图片，并通过 SKU 入库按钮确认")

    # 多套方案必须明确选中；只有一套时可直接确认唯一方案。
    if node == "c2" and resume_values.get("decision", "confirm") == "confirm":
        selected = resume_values.get("selected_scheme_indices")
        if selected is None:
            schemes = (graph_state.get("node2") or {}).get("schemes") or []
            if len(schemes) == 1:
                selected = resume_values["selected_scheme_indices"] = [0]
        if (not isinstance(selected, list) or len(selected) != 1
                or type(selected[0]) is not int
                or not 0 <= selected[0] < len((graph_state.get("node2") or {}).get("schemes") or [])):
            raise HTTPException(400, "请先查看商拍方案卡片，选 1 套您满意的方案后再点击确认。")
        counts = resume_values.get("per_scheme_count")
        if (not isinstance(counts, list) or len(counts) != len(selected)
                or any(type(count) is not int or count < 1 for count in counts)):
            raise HTTPException(400, "请由前端提交所选方案的提示词数量 per_scheme_count。")

    q = await launch_graph(task_id, _get_graph(), _langgraph_config(task_id),
                           command=Command(resume=resume_values))

    async def event_generator():
        # 首 event：resume 已接收
        yield _sse("resume_ack", {"task_id": task_id, "node": node, "message": "resume 已接收，后台继续执行"})

        last_event_ts = time.time()
        while True:
            if await request.is_disconnected():
                log_message(f"[sse-resume] task={task_id} 前端断开", page='对话', business='任务管理', status='记录')
                break

            try:
                event = await asyncio.wait_for(q.get(), timeout=settings.sse_heartbeat_interval_seconds)
                last_event_ts = time.time()
                event_type = event.get("type")
                event_data = event.get("data", {})

                yield _handle_sse_event(event_type, event_data)

                if event_type == "done" or event_type == "error":
                    break
                if event_type == "phase" and event_data.get("phase") == "failed":
                    break
                # interrupt 表示 graph 停在 HITL，这一轮 SSE 关闭
                if event_type == "interrupt":
                    log_message(f"[sse-resume] task={task_id} interrupt(node={event_data.get('node')})，关闭 SSE", page='对话', business='任务管理', status='记录')
                    break

            except asyncio.TimeoutError:
                elapsed = time.time() - last_event_ts
                if elapsed >= settings.sse_heartbeat_interval_seconds:
                    yield ": heartbeat\n\n"
                    last_event_ts = time.time()

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ===========================================================================
# restart_task —— 恢复"执行中"但 checkpoint 已经 stale 的 graph（SSE 直推）
# ===========================================================================


async def _check_product_images_ready(task) -> tuple[bool, str | None]:
    """检查 task 能否访问到商品图文件。

    request.product_images 存的是相对路径如 "uploads/{task_id}/p0.png"，
    需要拼上 settings.upload_dir（绝对路径前缀）才能找到真实文件。

    Returns:
        (ok, error_msg): ok=True → 商品图全部落盘可访问；ok=False → 有图路径但至少一个文件不存在，
            error_msg 带原因（文件缺失路径）。request.product_images 为空也返回 (True, None)
            （这种情况理论上不会发生在 graph 启动之后，但防御性处理）。
    """
    import asyncio
    from pathlib import Path
    from wellflow.app.config import settings

    req = task.request_json or {}
    paths: list[str] = req.get("product_images") or []
    if not paths:
        return True, None

    upload_root = Path(settings.upload_dir)

    def _resolve_and_check(rel: str) -> tuple[str, bool]:
        # rel 形如 "uploads/taskX/p0.png" → 去掉 "uploads/" 前缀后拼 upload_root
        stripped = rel
        if stripped.startswith("uploads/"):
            stripped = stripped[len("uploads/"):]
        real_path = upload_root / stripped
        return rel, real_path.exists()

    missing: list[str] = []
    results = await asyncio.gather(*(asyncio.to_thread(_resolve_and_check, p) for p in paths))
    for rel, ok in results:
        if not ok:
            missing.append(rel)

    if missing:
        return False, f"商品图文件缺失: {missing[:3]}{'...' if len(missing) > 3 else ''}"
    return True, None


@router.post("/{task_id}/restart", summary="重启执行中但已挂住的任务（SSE 直推）")
@page_context('对话')
async def restart_task(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """让卡住的任务重新跑起来。

    与 resume 的区别：resume 要求当前停在 interrupt 并携带 resume_values；
    restart 是让执行中途（node1/node2/node3/node4 某个计算节点）因为
    后端进程重启 / 崩溃而中断的 graph 从 checkpoint 续跑。
    内部就是 graph.astream(None) 从 snapshot 继续往下走到下一个 interrupt。
    """
    from wellflow.app.event_bus import is_running as _is_running
    from wellflow.app.graph_context import check_graph_runtime_state

    repo = TaskRepo(db)
    task = repo.get(task_id)
    if not task:
        raise HTTPException(404, f"task {task_id} 不存在")

    if task.phase == "archive_pending":
        raise HTTPException(409, "请重试完成图片入库")
    graph_state, snapshot = await _aget_graph_state(task_id)
    if graph_state is None:
        raise HTTPException(503, "无法读取工作流状态，请稍后重试")
    rt_state, rt_age = check_graph_runtime_state(snapshot)
    if rt_state == "none":
        raise HTTPException(409, "任务 checkpoint 缺失，请重新创建任务")
    if rt_state == "done":
        from wellflow.app.graph_persist import reconcile_checkpoint
        await asyncio.to_thread(reconcile_checkpoint, task_id, snapshot)
        async def completed():
            yield _sse("done", {"task_id": task_id, "phase": "done"})
        return StreamingResponse(completed(), media_type="text/event-stream")
    if not snapshot.next:
        raise HTTPException(409, "没有可恢复的执行节点，请重新创建任务")
    q = await launch_graph(task_id, _get_graph(), _langgraph_config(task_id))

    async def event_generator():
        yield _sse("restart_ack", {
            "task_id": task_id,
            "phase": task.phase,
            "rt_state": rt_state,
            "message": "已发起 graph 重启，从 checkpoint 续跑",
        })

        while True:
            if await request.is_disconnected():
                log_message(f"[sse-restart] task={task_id} 前端断开", page='对话', business='任务管理', status='记录')
                break

            try:
                event = await asyncio.wait_for(q.get(), timeout=settings.sse_heartbeat_interval_seconds)
                event_type = event.get("type")
                event_data = event.get("data", {})

                yield _handle_sse_event(event_type, event_data)

                if event_type == "done" or event_type == "error":
                    break
                if event_type == "phase" and event_data.get("phase") == "failed":
                    break
                if event_type == "interrupt":
                    # graph 正常停下来等用户确认了，关闭 SSE
                    log_message(f"[sse-restart] task={task_id} interrupt(node={event_data.get('node')})，关闭 SSE", page='对话', business='任务管理', status='记录')
                    break
            except asyncio.TimeoutError:
                yield _sse("ping", {"ts": int(time.time())})

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# 列表 + 单任务查询（保持不变，兼容性）
# ===========================================================================


@router.get("", response_model=StandardResponse[TaskListResponse], summary="列出任务（分页）")
@page_context('对话')
def list_tasks(
    page: int = 1,
    page_size: int = 20,
    phase: str | None = None,
    db: Session = Depends(get_db),
):
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 100:
        page_size = 20

    repo = TaskRepo(db)
    items, total = repo.list_tasks(page=page, page_size=page_size, phase=phase)

    list_items: list[TaskListItem] = []
    for t in items:
        req = t.request_json or {}
        list_items.append(TaskListItem(
            task_id=t.task_id,
            phase=canonical_phase(t.phase),
            platform=req.get("platform", ""),
            marketing_goal=req.get("marketing_goal", ""),
            description=req.get("description", "")[:80],
            has_interrupt=bool(t.interrupt_json),
            created_at=to_cn_iso(t.created_at),
            updated_at=to_cn_iso(t.updated_at),
        ))

    return ok(TaskListResponse(
        items=list_items,
        total=total,
        page=page,
        page_size=page_size,
    ))


async def _aget_graph_state(task_id: str) -> tuple[dict[str, Any] | None, Any]:
    """返回 (snapshot.values, snapshot) —— resolve_current_node 要读 snapshot.next。"""
    try:
        graph = _get_graph()
        if graph is None:
            return None, None
        config = _langgraph_config(task_id)
        snapshot = await graph.aget_state(config)
        if snapshot is None or not hasattr(snapshot, "values"):
            return None, snapshot
        return snapshot.values, snapshot
    except Exception as exc:
        log_message(f"[get_task] 从 checkpoint 读取 state 失败: {exc}", page='对话', business='任务管理', status='记录')
        return None, None


@router.get("/{task_id}", response_model=StandardResponse[TaskInfoResponse], summary="查询任务状态")
@page_context('对话')
async def get_task(task_id: str, db: Session = Depends(get_db)):
    """查询单任务状态（从 DB + LangGraph checkpoint）。保留给旧版客户端用。"""
    from wellflow.app.schemas.task_schemas import TaskInfoResponse
    repo = TaskRepo(db)
    task = repo.get(task_id)
    if not task:
        raise HTTPException(404, f"task {task_id} 不存在")

    graph_state, snapshot = await _aget_graph_state(task_id)
    node1 = graph_state.get("node1") if graph_state else None
    node2 = graph_state.get("node2") if graph_state else None
    node3 = graph_state.get("node3") if graph_state else None
    node4 = graph_state.get("node4") if graph_state else None

    if graph_state is None:
        raise HTTPException(503, "无法读取工作流状态，请稍后重试")
    if isinstance(node1, dict) and node1.get("product_insight") and not node1.get("report_sections"):
        from wellflow.app.prompt.report_sections import build_report_sections
        node1 = {**node1, "report_sections": build_report_sections(node1["product_insight"])}
    from wellflow.app.workflow_status import checkpoint_view
    from wellflow.app.graph_context import check_graph_runtime_state
    from wellflow.app.event_bus import is_running
    phase, interrupt_json = checkpoint_view(snapshot)
    recovery_error = ("任务 checkpoint 缺失，无法恢复，请创建新任务" if phase == "missing" else
                      "没有可恢复的执行节点，请创建新任务" if phase == "needs_retry" else None)
    runtime_status, _ = check_graph_runtime_state(snapshot)
    can_restart = bool(snapshot and snapshot.next and not interrupt_json and not is_running(task_id))
    if phase == "missing":
        phase, runtime_status, can_restart = "needs_retry", "missing", False
    if is_running(task_id):
        phase, interrupt_json, runtime_status, can_restart = "processing", None, "running", False
    if task.phase == "done":
        phase, interrupt_json, runtime_status, can_restart = "done", None, "done", False
    if task.phase == "archive_pending":
        phase, can_restart = "archive_pending", False
    # A read never overwrites a newer execution's DB projection. Mutations repair
    # it under the execution lease before consuming a command.
    if graph_state:
        from wellflow.app.generation_journal import restore_completed
        node4 = await asyncio.to_thread(restore_completed, task_id, node4, graph_state.get("workflow_revision", 0))

    # 从 task_image 表读出参考图（按 type 分组）+ 生图成品
    reference_images: dict[str, list[dict[str, Any]]] = {"mannequin": [], "scene": [], "outfit": []}
    output_images: list[dict[str, Any]] = []
    for img in repo.list_images(task_id):
        item: dict[str, Any] = {
            "image_id": img.image_id,
            "storage_uri": img.storage_uri,
            "url": _storage_uri_url(img.storage_uri),
        }
        t = img.image_type or ""
        if t in reference_images:
            reference_images[t].append(item)
        elif t == "model":  # 兼容老数据里的 "model" type → 归到 mannequin
            reference_images["mannequin"].append(item)
        else:
            item["shot_id"] = img.shot_id
            item["prompt"] = img.prompt
            item["prompt_index"] = img.prompt_index
            output_images.append(item)

    # Current selection is checkpoint-owned; task_image is historical only.
    reference_images = {
        kind: [{"storage_uri": uri, "url": _storage_uri_url(uri)}
               for uri in ((node3 or {}).get("reference_images") or {}).get(kind, [])]
        for kind in ("mannequin", "scene", "outfit")
    }

    from wellflow.app.services.sku_archive import with_image_keys
    from wellflow.app.services.task_presentation import public_task_node
    if interrupt_json:
        interrupt_json = with_image_keys(interrupt_json)
    return ok(TaskInfoResponse(
        task_id=task.task_id,
        phase=phase,
        runtime_status=runtime_status,
        can_restart=can_restart,
        recovery_error=recovery_error,
        request=graph_state.get("request") or task.request_json or {},
        selected_plan_ids=task.selected_plan_ids_json or [],
        interrupt=interrupt_json,
        cost=graph_state.get("cost", {}) if graph_state else {},
        progress=graph_state.get("progress", {}) if graph_state else {},
        node1=public_task_node(node1),
        node2=public_task_node(node2),
        node3=public_task_node(node3),
        node4=public_task_node(node4, generation=True),
        reference_images=reference_images,
        output_images=output_images,
        created_at=to_cn_iso(task.created_at),
        updated_at=to_cn_iso(task.updated_at),
    ))


# ===========================================================================
# delete_task —— 彻底删除任务（DB + 磁盘文件 + LangGraph checkpoint）
# ===========================================================================


@router.delete("/{task_id}", response_model=StandardResponse[dict], summary="删除任务及所有关联数据")
@page_context('对话')
async def delete_task(task_id: str):
    """删除指定任务的全部数据：DB 所有子表记录 + 主表 + uploads/{task_id}/ 磁盘文件 + LangGraph checkpoint。

    仅允许删除终态任务（done / failed / needs_retry），进行中的任务返回 409 Conflict。
    """
    from wellflow.app.runtime import get_checkpointer

    # 1. 先查任务是否存在 + 是否处于可删除状态
    with session_scope() as db:
        repo = TaskRepo(db)
        task = repo.get(task_id)
        if not task:
            raise HTTPException(404, f"task {task_id} 不存在")

        if task.phase == "archive_pending":
            raise HTTPException(409, "请先重试完成图片入库，再删除任务")

        # 只有 node1/node2/node3 正在执行时才禁止删除；
        # HITL 等待（c1_confirm / c2_confirm）和终态（done / failed / needs_retry）都允许删
        # 关键：后端重启后 DB phase 可能陈旧（还停在 input 但 checkpoint 已经 done/paused），
        # 必须用 checkpoint 状态二次确认是否真在跑
        _ACTIVE_PHASES = {
            TaskPhase.INPUT.value,
            TaskPhase.RESEARCH.value,
            TaskPhase.PLANNING.value,
            TaskPhase.DELIVERY.value,
        }
        if task.phase in _ACTIVE_PHASES:
            # DB phase 看起来 active → 再看 checkpoint 是不是真在执行
            _actually_running = True  # 保守默认：读不到 checkpoint 就拦
            try:
                from wellflow.app.graph_context import check_graph_runtime_state as _check_rt
                _, snapshot = await _aget_graph_state(task_id)
                rt_state, _ = _check_rt(snapshot)
                # running/stale = 真在跑；done/paused/none = 没有在执行的 graph
                _actually_running = rt_state in ("running", "stale")
            except Exception as _ckpt_err:
                log_message(f"[delete_task] ⚠️ checkpoint 状态读不到，保守按 DB phase 拦截: {_ckpt_err}", page='对话', business='删除商拍任务', status='警告')

            if _actually_running:
                raise HTTPException(
                    409,
                    f"任务正在执行中（phase='{task.phase}'），请等待执行完成或人工确认后再删除",
                )
            log_message(f"[delete_task] 🛡️ DB phase={task.phase} 但 checkpoint 已停（rt_state={rt_state!r}），"
                f"允许删除", page='对话', business='删除商拍任务', status='记录')

        # 2. 删 DB（所有子表 + 主表）
        repo.delete_task(task_id)

    # 3. 删磁盘文件（uploads/{task_id}/）
    from wellflow.app.utils.image_store import delete_task_files
    delete_task_files(task_id)
    from wellflow.app.newapi.pool import clear_task_models
    clear_task_models(task_id)

    # 4. 删 LangGraph checkpoint（thread_id = task_id）
    cp = get_checkpointer()
    if cp is not None and hasattr(cp, "adelete_thread"):
        try:
            await cp.adelete_thread(task_id)
            log_message(f"[delete_task] ✅ checkpoint 已清理 task_id={task_id}", page='对话', business='删除商拍任务', status='成功')
        except Exception as e:
            log_message(f"[delete_task] ⚠️ checkpoint 清理失败（不影响主流程）: {e}", page='对话', business='删除商拍任务', status='警告')

    log_message(f"[delete_task] 🗑️ 任务已彻底删除 task_id={task_id}", page='对话', business='删除商拍任务', status='记录')
    return ok({"task_id": task_id, "deleted": True})
