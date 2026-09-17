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
from wellflow.app.api.utils import ok, StandardResponse
from wellflow.app.graph_persist import persist_phase, persist_interrupt, persist_error, persist_outputs
from wellflow.app.models.task_models import TaskPhase
from wellflow.app.repositories.task_repo import TaskRepo
from wellflow.app.schemas.task_schemas import (
    TaskListResponse,
    TaskListItem,
    TaskInfoResponse,
)


router = APIRouter(prefix="/tasks", tags=["任务"])


def _short_uuid() -> str:
    return uuid.uuid4().hex[:12]


def _get_graph():
    from wellflow.app.main import get_graph
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
# graph chunk 处理（和旧版一样，推 SSE + fire-and-forget 写 DB）
# ---------------------------------------------------------------------------


def _handle_graph_chunk(task_id: str, chunk: Any) -> None:
    """处理 graph.astream 的单个 chunk：先推 SSE（零延迟），再异步写 DB。"""
    from wellflow.app.event_bus import publish as _eb
    print(f"[graph-chunk] task={task_id} type={type(chunk).__name__} chunk={repr(chunk)[:400]}", flush=True)

    interrupt_value = _extract_interrupt_value(chunk)
    if interrupt_value:
        print(f"[graph-interrupt] ✅ 命中 interrupt! node={interrupt_value.get('node')}", flush=True)
        phase = interrupt_value.get("phase") or f"{interrupt_value.get('node')}_confirm"
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
            if node_name == "__interrupt__":
                continue
            if not isinstance(node_out, dict):
                continue
            phase = node_out.get("phase")
            if not phase:
                continue
            _eb(task_id, "phase", {"phase": phase})
            if phase == "done":
                n4_raw = node_out.get("node4") or {}
                n3_raw = node_out.get("node3") or {}
                if n4_raw.get("reference_images"):
                    from wellflow.app.utils.image_store import paths_to_data_uris
                    n4_raw = {**n4_raw, "reference_images": paths_to_data_uris(n4_raw["reference_images"])}
                _eb(task_id, "done", {
                    "phase": "done",
                    "node1": node_out.get("node1"),
                    "node2": node_out.get("node2"),
                    "node3": n3_raw,
                    "node4": n4_raw,
                    "cost": node_out.get("cost", {}),
                    "progress": node_out.get("progress", {}),
                })
                # 确认结束：生图成品落盘 + 写 task_image 表（按 task_id 可查）
                asyncio.get_event_loop().run_in_executor(
                    None, persist_outputs, task_id, n4_raw
                )
            asyncio.get_event_loop().run_in_executor(
                None, persist_phase, task_id, phase, node_name
            )


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


# ---------------------------------------------------------------------------
# 核心优化：异步启动 graph（fire-and-forget）
# ---------------------------------------------------------------------------


async def _start_graph(task_id: str, graph, config, initial_state=None, resume_values=None):
    """后台启动 LangGraph，异常转成 SSE error 事件。"""
    from wellflow.app.event_bus import publish as _eb, cleanup as _eb_cleanup
    import traceback as _tb
    try:
        if resume_values is not None:
            stream_iter = graph.astream(
                Command(resume=resume_values), config=config,
                stream_mode=["updates"],
            )
        else:
            stream_iter = graph.astream(
                initial_state, config=config,
                stream_mode=["updates"],
            )
        async for chunk in stream_iter:
            _handle_graph_chunk(task_id, chunk)
    except Exception as exc:
        print(f"[graph] ❌ task_id={task_id} error={exc}", flush=True)
        _tb.print_exc()
        try:
            _eb(task_id, "error", {"phase": "failed", "message": str(exc)})
            asyncio.get_event_loop().run_in_executor(
                None, persist_error, task_id, "GRAPH_RUNTIME_ERROR", str(exc), "parent_graph"
            )
        except Exception:
            pass
    finally:
        _eb_cleanup(task_id)


# ===========================================================================
# create_task —— 现在返回 StreamingResponse（SSE），一个 HTTP 搞定
# ===========================================================================


@router.post("", summary="创建商拍任务（SSE 直推，零延迟）")
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
    print(f"[create_task] 🚀 开始, task_id={task_id}", flush=True)

    # --- Step 1: 读上传文件 → 落盘（同步 I/O，放 to_thread 里更快？不，FastAPI UploadFile.read 是 async 的） ---
    raw_files = [(f.filename or "image", await f.read(), f.content_type) for f in product_images]
    from wellflow.app.utils.image_store import save_upload
    product_image_paths = save_upload(task_id, raw_files, prefix="p")
    print(f"[create_task] 📁 图片落盘完成 ({time.time() - t0:.2f}s), paths={len(product_image_paths)}", flush=True)

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
        asyncio.create_task(_create_task_in_background(task_id, request_json, brand_cfg, graph, config, initial_state))
    except Exception as exc:
        print(f"[create_task] ❌ graph 不可用: {exc}", flush=True)

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

        # 先推一个 phase=input（对齐旧版 SSE 契约）
        yield _sse("phase", {"phase": "input"})

        # 主循环：从 queue 读事件推给前端
        _HEARTBEAT_INTERVAL = 25  # 秒
        last_event_ts = time.time()
        while True:
            if await request.is_disconnected():
                print(f"[sse] task={task_id} 前端断开连接", flush=True)
                break

            # 带超时的 get，用于 heartbeat
            try:
                event = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_INTERVAL)
                last_event_ts = time.time()
                event_type = event.get("type")
                event_data = event.get("data", {})
                print(f"[sse] task={task_id} ← queue got type={event_type}", flush=True)

                # 处理各类事件
                yield _handle_sse_event(event_type, event_data)

                # 终态：done / error → 结束 SSE 流
                if event_type == "done" or (event_type == "phase" and event_data.get("phase") == "failed"):
                    print(f"[sse] task={task_id} 终态到达，关闭 SSE", flush=True)
                    break
                if event_type == "error":
                    print(f"[sse] task={task_id} error，关闭 SSE", flush=True)
                    break
                # interrupt 表示 graph 停在 HITL 等待用户输入，这一轮 SSE 应该关闭
                # 前端下一次 resume 会发新的 HTTP 请求开启新 SSE 流
                if event_type == "interrupt":
                    print(f"[sse] task={task_id} interrupt(node={event_data.get('node')})，关闭 SSE（等待用户确认）", flush=True)
                    break

            except asyncio.TimeoutError:
                # heartbeat
                elapsed = time.time() - last_event_ts
                if elapsed >= _HEARTBEAT_INTERVAL:
                    yield ": heartbeat\n\n"
                    last_event_ts = time.time()

    print(f"[create_task] ✅ 耗时 {time.time() - t0:.2f}s，返回 StreamingResponse", flush=True)
    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def _create_task_in_background(
    task_id: str,
    request_json: dict[str, Any],
    brand_cfg: dict[str, Any],
    graph,
    config,
    initial_state: dict[str, Any],
):
    """后台任务：先写 DB（必须成功），再启动 graph。

    DB create 必须在 graph 启动前完成——否则 graph 很快跑到 interrupt 点，
    前端立即调 resume，此时 Task 行还没写入就会 404。
    """
    def _sync_write():
        with session_scope() as db:
            repo = TaskRepo(db)
            repo.create(task_id=task_id, request_json=request_json, phase=TaskPhase.INPUT.value, brand_config_json=brand_cfg)
            repo.add_event(task_id, "task_created", phase=TaskPhase.INPUT.value, payload_json=request_json)

    try:
        await asyncio.to_thread(_sync_write)
    except Exception as e:
        from wellflow.app.event_bus import publish as _eb
        print(f"[create_task] ❌ DB 写入失败: {e}", flush=True)
        _eb(task_id, "error", {"phase": "failed", "message": f"任务创建失败（DB）: {e}"})
        return  # 不启动 graph

    # DB 就绪，启动 graph
    await _start_graph(task_id, graph, config, initial_state=initial_state)


def _handle_sse_event(event_type: str, event_data: dict[str, Any]) -> str:
    """把 event_bus 事件转成 SSE 字符串。"""
    if event_type == "phase":
        return _sse("phase", {"phase": event_data.get("phase")})
    elif event_type == "interrupt":
        return _sse("interrupt", event_data)
    elif event_type == "thinking_chunk":
        return _sse("thinking_chunk", event_data)
    elif event_type == "report_chunk":
        return _sse("report_chunk", event_data)
    elif event_type == "report_chunk_done":
        return _sse("report_chunk_done", event_data)
    elif event_type == "node4_image_done":
        return _sse("node4_image_done", event_data)
    elif event_type == "node4_image_failed":
        return _sse("node4_image_failed", event_data)
    elif event_type == "scheme_chunk":
        return _sse("scheme_chunk", event_data)
    elif event_type == "scheme_chunk_done":
        return _sse("scheme_chunk_done", event_data)
    elif event_type == "prompt_chunk":
        return _sse("prompt_chunk", event_data)
    elif event_type == "prompt_chunk_done":
        return _sse("prompt_chunk_done", event_data)
    elif event_type == "done":
        return _sse("done", event_data)
    elif event_type == "error":
        return _sse("error", event_data)
    else:
        return ""  # 未知事件类型，忽略


# ===========================================================================
# resume_task —— 也改成返回 StreamingResponse
# ===========================================================================


@router.post("/{task_id}/resume", summary="从 HITL 点恢复任务（SSE 直推）")
async def resume_task(
    task_id: str,
    request: Request,
    node: str = Form(...),
    confirmed_report: str = Form(default=""),
    ratio: str = Form(default="9:16"),
    scheme_count: int = Form(default=3),
    action: str = Form(default="confirm"),
    image_model: str | None = Form(default=None),
    model_images: list[UploadFile] = File(default_factory=list),
    # C2（选方案）：selected_scheme_indices "0,2" 或 JSON body
    selected_scheme_indices: str | None = Form(default=None),
    # C3（确认 prompt）：JSON body，后端不拆 Form
    # C4（重做/确认）：decision "redo" / "confirm"，redo_target 可选
    redo_target: str | None = Form(default=None),
    # 🔑 灵活 JSON body：前端可直接传完整 resume_values dict
    # 优先级最高，覆盖所有 Form 字段
    resume_json: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    import json as _json

    repo = TaskRepo(db)
    task = repo.get(task_id)
    if not task:
        raise HTTPException(404, f"task {task_id} 不存在")

    interrupt = task.interrupt_json or {}
    current_node = interrupt.get("node")
    if not current_node or current_node != node:
        raise HTTPException(409, f"当前暂停在 node={current_node}，不能 resume node={node}")

    # model_images 落盘（C1 专用）
    model_image_paths: list[str] = []
    if model_images:
        raw_files = [(f.filename or "model", await f.read(), f.content_type) for f in model_images]
        from wellflow.app.utils.image_store import save_upload
        model_image_paths = save_upload(task_id, raw_files, prefix="m")

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
        if model_image_paths and "model_images" not in resume_values:
            resume_values["model_images"] = model_image_paths
    else:
        # ---- 按 node 类型组装 Form 参数 ----
        resume_values: dict[str, Any] = {"node": node}
        if node == "c1":
            # C1：报告确认，也支持 redo 模式
            if action and action != "confirm":
                resume_values["decision"] = action  # "redo"
                resume_values["redo_target"] = redo_target or "node1"
                print(f"[resume] C1 redo → {redo_target or 'node1'}", flush=True)
            else:
                # C1 阶段无需传模特参考图，用户直接确认报告即可继续
                resume_values["confirmed_report"] = confirmed_report
                if model_image_paths:
                    resume_values["model_images"] = model_image_paths
                resume_values["ratio"] = ratio
                if image_model:
                    resume_values["image_model"] = image_model
        elif node == "c2":
            # C2：选方案，selected_scheme_indices
            # 同时支持 redo 模式：action="redo" + redo_target="node1"/"node2"
            if action and action != "confirm":
                resume_values["decision"] = action  # "redo"
                if redo_target:
                    resume_values["redo_target"] = redo_target
                print(f"[resume] C2 redo → {redo_target}", flush=True)
            else:
                if image_model:
                    resume_values["image_model"] = image_model
                if ratio:
                    resume_values["ratio"] = ratio
                if selected_scheme_indices:
                    try:
                        indices = [int(x) for x in selected_scheme_indices.split(",") if x.strip()]
                        resume_values["selected_scheme_indices"] = indices
                        print(f"[resume] C2 收到 selected_scheme_indices={indices}", flush=True)
                    except ValueError:
                        print(f"[resume] ⚠️ selected_scheme_indices 解析失败: {selected_scheme_indices}", flush=True)
        elif node == "c3":
            # C3：确认提示词 — Form 不够用，前端应传 resume_json
            # 同时支持 redo 模式：action="redo" + redo_target="node1"/"node2"/"node3"/"node4"
            if action and action != "confirm":
                resume_values["decision"] = action  # "redo"
                if redo_target:
                    resume_values["redo_target"] = redo_target
                print(f"[resume] C3 redo → {redo_target}", flush=True)
            else:
                if image_model:
                    resume_values["image_model"] = image_model
                if ratio:
                    resume_values["ratio"] = ratio
        elif node == "c4":
            # C4：重做/确认 — decision "confirm"/"redo" + redo_target
            resume_values["decision"] = action  # "confirm" / "redo"
            if redo_target:
                resume_values["redo_target"] = redo_target

    # fire-and-forget DB: 清 interrupt + 写事件
    def _sync_prepare():
        try:
            with session_scope() as db:
                repo2 = TaskRepo(db)
                repo2.save_interrupt(task_id, None)
                repo2.add_event(
                    task_id, "task_resumed",
                    payload_json={
                        "node": node,
                        "model_image_count": len(model_image_paths),
                        "action": action if node == "c3" else None,
                    }
                )
                # 模特图路径持久化到 task_image（按 task_id 可查）
                if node == "c1" and model_image_paths:
                    repo2.save_images(task_id, [
                        {"image_type": "model", "storage_uri": p}
                        for p in model_image_paths
                    ])
        except Exception as e:
            print(f"[resume] ⚠️ DB prepare 失败: {e}", flush=True)

    asyncio.get_event_loop().run_in_executor(None, _sync_prepare)

    # 注册 event_bus queue + 启动 graph
    from wellflow.app.event_bus import drain_and_subscribe
    q = await drain_and_subscribe(task_id)

    try:
        graph = _get_graph()
        config = _langgraph_config(task_id)
        asyncio.create_task(_start_graph(task_id, graph, config, resume_values=resume_values))
    except Exception as exc:
        raise HTTPException(503, f"graph 不可用: {exc}")

    async def event_generator():
        # 首 event：resume 已接收
        yield _sse("resume_ack", {"task_id": task_id, "node": node, "message": "resume 已接收，后台继续执行"})

        _HEARTBEAT_INTERVAL = 25
        last_event_ts = time.time()
        while True:
            if await request.is_disconnected():
                print(f"[sse-resume] task={task_id} 前端断开", flush=True)
                break

            try:
                event = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_INTERVAL)
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
                    print(f"[sse-resume] task={task_id} interrupt(node={event_data.get('node')})，关闭 SSE", flush=True)
                    break

            except asyncio.TimeoutError:
                elapsed = time.time() - last_event_ts
                if elapsed >= _HEARTBEAT_INTERVAL:
                    yield ": heartbeat\n\n"
                    last_event_ts = time.time()

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ===========================================================================
# 列表 + 单任务查询（保持不变，兼容性）
# ===========================================================================


@router.get("", response_model=StandardResponse[TaskListResponse], summary="列出任务（分页）")
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
            phase=t.phase,
            platform=req.get("platform", ""),
            marketing_goal=req.get("marketing_goal", ""),
            description=req.get("description", "")[:80],
            has_interrupt=bool(t.interrupt_json),
            created_at=t.created_at.isoformat(),
            updated_at=t.updated_at.isoformat(),
        ))

    return ok(TaskListResponse(
        items=list_items,
        total=total,
        page=page,
        page_size=page_size,
    ))


async def _aget_graph_state(task_id: str) -> dict[str, Any] | None:
    try:
        graph = _get_graph()
        if graph is None:
            return None
        config = _langgraph_config(task_id)
        snapshot = await graph.aget_state(config)
        if snapshot is None or not hasattr(snapshot, "values"):
            return None
        return snapshot.values
    except Exception as exc:
        print(f"[get_task] 从 checkpoint 读取 state 失败: {exc}", flush=True)
        return None


@router.get("/{task_id}", response_model=StandardResponse[TaskInfoResponse], summary="查询任务状态")
async def get_task(task_id: str, db: Session = Depends(get_db)):
    """查询单任务状态（从 DB + LangGraph checkpoint）。保留给旧版客户端用。"""
    from wellflow.app.schemas.task_schemas import TaskInfoResponse
    repo = TaskRepo(db)
    task = repo.get(task_id)
    if not task:
        raise HTTPException(404, f"task {task_id} 不存在")

    graph_state = await _aget_graph_state(task_id)
    node1 = graph_state.get("node1") if graph_state else None
    node2 = graph_state.get("node2") if graph_state else None
    node3 = graph_state.get("node3") if graph_state else None

    # 从 task_image 表读出模特图 + 生图成品（按 task_id 查询）
    model_images: list[dict[str, Any]] = []
    output_images: list[dict[str, Any]] = []
    for img in repo.list_images(task_id):
        item: dict[str, Any] = {
            "image_id": img.image_id,
            "storage_uri": img.storage_uri,
            "url": _storage_uri_url(img.storage_uri),
        }
        if img.image_type == "model":
            model_images.append(item)
        else:
            item["shot_id"] = img.shot_id
            item["prompt"] = img.prompt
            item["prompt_index"] = img.prompt_index
            item["variant_index"] = img.variant_index
            output_images.append(item)

    return ok(TaskInfoResponse(
        task_id=task.task_id,
        phase=task.phase,
        request=task.request_json or {},
        selected_plan_ids=task.selected_plan_ids_json or [],
        interrupt=task.interrupt_json,
        cost=graph_state.get("cost", {}) if graph_state else {},
        progress=graph_state.get("progress", {}) if graph_state else {},
        node1=node1,
        node2=node2,
        node3=node3,
        model_images=model_images,
        output_images=output_images,
        created_at=task.created_at.isoformat(),
        updated_at=task.updated_at.isoformat(),
    ))


# ===========================================================================
# delete_task —— 彻底删除任务（DB + 磁盘文件 + LangGraph checkpoint）
# ===========================================================================


@router.delete("/{task_id}", response_model=StandardResponse[dict], summary="删除任务及所有关联数据")
async def delete_task(task_id: str):
    """删除指定任务的全部数据：DB 所有子表记录 + 主表 + uploads/{task_id}/ 磁盘文件 + LangGraph checkpoint。

    仅允许删除终态任务（done / failed / needs_retry），进行中的任务返回 409 Conflict。
    """
    from wellflow.app.main import get_checkpointer

    # 1. 先查任务是否存在 + 是否处于可删除状态
    with session_scope() as db:
        repo = TaskRepo(db)
        task = repo.get(task_id)
        if not task:
            raise HTTPException(404, f"task {task_id} 不存在")

        # 只有 node1/node2/node3 正在执行时才禁止删除；
        # HITL 等待（c1_confirm / c2_confirm）和终态（done / failed / needs_retry）都允许删
        _ACTIVE_PHASES = {
            TaskPhase.INPUT.value,
            TaskPhase.RESEARCH.value,
            TaskPhase.PLANNING.value,
            TaskPhase.DELIVERY.value,
        }
        if task.phase in _ACTIVE_PHASES:
            raise HTTPException(
                409,
                f"任务正在执行中（phase='{task.phase}'），请等待执行完成或人工确认后再删除",
            )

        # 2. 删 DB（所有子表 + 主表）
        repo.delete_task(task_id)

    # 3. 删磁盘文件（uploads/{task_id}/）
    from wellflow.app.utils.image_store import delete_task_files
    delete_task_files(task_id)

    # 4. 删 LangGraph checkpoint（thread_id = task_id）
    cp = get_checkpointer()
    if cp is not None and hasattr(cp, "adelete_thread"):
        try:
            await cp.adelete_thread(task_id)
            print(f"[delete_task] ✅ checkpoint 已清理 task_id={task_id}", flush=True)
        except Exception as e:
            print(f"[delete_task] ⚠️ checkpoint 清理失败（不影响主流程）: {e}", flush=True)

    print(f"[delete_task] 🗑️ 任务已彻底删除 task_id={task_id}", flush=True)
    return ok({"task_id": task_id, "deleted": True})
