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
from wellflow.app.api.utils import ok, StandardResponse, to_cn_iso
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


async def _handle_graph_chunk(task_id: str, chunk: Any) -> None:
    """处理 graph.astream 的单个 chunk：先推 SSE，再按流顺序写 DB。

    DB 写入用 await asyncio.to_thread 串行化——fire-and-forget 的 run_in_executor
    不保证执行顺序，曾出现 persist_phase("node1_vlm_done") 晚于
    persist_interrupt("c1_confirm") 提交、把 task.phase 回退成旧值的竞态。
    """
    from wellflow.app.event_bus import publish as _eb
    print(f"[graph-chunk] task={task_id} type={type(chunk).__name__} chunk={repr(chunk)[:400]}", flush=True)

    interrupt_value = _extract_interrupt_value(chunk)
    if interrupt_value:
        print(f"[graph-interrupt] ✅ 命中 interrupt! node={interrupt_value.get('node')}", flush=True)
        phase = interrupt_value.get("phase") or f"{interrupt_value.get('node')}_confirm"
        _eb(task_id, "phase", {"phase": phase})
        _eb(task_id, "interrupt", {**interrupt_value, "_phase": phase})
        await asyncio.to_thread(persist_interrupt, task_id, interrupt_value, phase)
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
                await asyncio.to_thread(persist_outputs, task_id, n4_raw)
            await asyncio.to_thread(persist_phase, task_id, phase, node_name)


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
    from wellflow.app.event_bus import publish as _eb, cleanup as _eb_cleanup, mark_running, mark_done
    import traceback as _tb
    mark_running(task_id)
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
            await _handle_graph_chunk(task_id, chunk)
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
        mark_done(task_id)
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
    # action: "confirm" | "refine" | "redo"(仅 C4 redo→node4 保留)
    action: str = Form(default="confirm"),
    image_model: str | None = Form(default=None),
    model_images: list[UploadFile] = File(default_factory=list),
    selected_scheme_indices: str | None = Form(default=None),
    # redo_target: C4 阶段可选，仅 redo 模式生效
    redo_target: str | None = Form(default=None),
    # refine 专用字段：C1/C2/C3/C4 都支持
    refine_instruction: str | None = Form(default=None),
    refine_target: str | None = Form(default=None),
    resume_json: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    import json as _json

    repo = TaskRepo(db)
    task = repo.get(task_id)
    if not task:
        raise HTTPException(404, f"task {task_id} 不存在")

    # 终态守卫：确认入库（done）后禁止任何 redo / resume
    if task.phase == TaskPhase.DONE.value:
        raise HTTPException(409, "任务已确认入库，禁止任何重做操作")

    # 并发保护：graph 正在执行时禁止 resume（防止并发跑两个 graph 互相覆盖 interrupt_json）
    from wellflow.app.event_bus import is_running as _is_running
    if _is_running(task_id):
        raise HTTPException(409, "任务正在执行中，请等待完成后再操作")

    interrupt = task.interrupt_json or {}
    current_node = interrupt.get("node")

    # ------------------------------------------------------------------
    # phase vs interrupt_json 一致性校验（防并发损坏）
    #   偶发场景：并发 race 导致 interrupt_json.node 和 task.phase 不对。
    #   这里用 phase 作为权威来源反推当前 node（phase 是最常写、最安全的字段）。
    #   如果两者不一致，以 phase 为准并自动修复 interrupt_json。
    # ------------------------------------------------------------------
    _PHASE_TO_NODE: dict[str, str] = {
        "c1_confirm": "c1",
        "c2_select":  "c2",
        "c2_confirm": "c2",
        "c3_confirm": "c3",
        "c3":         "c3",
        "c4_review":  "c4",
    }
    # backward / redo 后 phase 可能残留旧值，这时候 interrupt_json 更可信
    expected_node_from_phase = _PHASE_TO_NODE.get(task.phase)
    if expected_node_from_phase and current_node != expected_node_from_phase:
        print(f"[resume] ⚠️ phase({task.phase}) 推导出 node={expected_node_from_phase}，"
              f"但 interrupt_json.node={current_node}，以 phase 为准自动修复", flush=True)
        repo.save_interrupt(task_id, {
            "node": expected_node_from_phase,
            "phase": task.phase,
        })
        db.commit()
        current_node = expected_node_from_phase

    if not current_node:
        raise HTTPException(409, "任务当前没有暂停点（interrupt_json 为空）")
    if current_node != node:
        raise HTTPException(409, f"当前暂停在 node={current_node}，不能 resume node={node}")

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
        graph_state, _ = await _aget_graph_state(task_id)
        ok, missing = validate_current_node_products(current_node, graph_state)
        if not ok:
            print(
                f"[resume] 🛡️ 硬性守卫拦截 current_node={current_node}，"
                f"缺失产物: {missing} → 拒绝 resume，请 retry 当前 node",
                flush=True,
            )
            raise HTTPException(
                409,
                f"当前节点({current_node})产物缺失或损坏({missing})，"
                "请重试当前节点，不允许跳过向下流转。",
            )

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
        _is_refine = (action == "refine")
        _is_redo = (action == "redo")

        # ---- 通用 refine / redo 拦截：C1/C2/C3 不支持完全 redo ----
        if _is_redo and node in ("c1", "c2", "c3"):
            print(f"[resume] ⚠️ node={node} 不支持完全重做（redo），自动降级为 refine", flush=True)
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
            print(f"[resume] node={node} refine target={refine_target or 'default'} "
                  f"instruction={refine_instruction.strip()}", flush=True)

        # redo 通用注入（仅 C4 redo→node4 保留）
        if _is_redo:
            if node != "c4":
                raise HTTPException(400, f"node={node} 不支持完全重做（redo），请使用 action=refine")
            target = redo_target or "node4"
            if target != "node4":
                raise HTTPException(400, f"C4 redo 仅支持 redo_target=node4，node2/node3 请使用 action=refine")
            resume_values["decision"] = "redo"
            resume_values["redo_target"] = target
            print(f"[resume] C4 redo → {target}", flush=True)

        # ---- confirm 模式：按 node 分支处理正常流转 ----
        if not _is_refine and not _is_redo:
            if node == "c1":
                resume_values["confirmed_report"] = confirmed_report
                if model_image_paths:
                    resume_values["model_images"] = model_image_paths
                resume_values["ratio"] = ratio
                if image_model:
                    resume_values["image_model"] = image_model
            elif node == "c2":
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
                _psc = request.form.get("per_scheme_count")
                if _psc:
                    try:
                        counts = [int(x) for x in _psc.split(",") if x.strip()]
                        resume_values["per_scheme_count"] = [max(1, c) for c in counts]
                        print(f"[resume] C2 收到 per_scheme_count={counts}", flush=True)
                    except ValueError:
                        print(f"[resume] ⚠️ per_scheme_count 解析失败: {_psc}", flush=True)
            elif node == "c3":
                if image_model:
                    resume_values["image_model"] = image_model
                if ratio:
                    resume_values["ratio"] = ratio
            elif node == "c4":
                resume_values["decision"] = "confirm"

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
                        "action": action,
                        "redo_target": redo_target,
                        "refine_target": refine_target,
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

    # 商品图前置检查：graph 跑 node1 必须读这些文件，文件缺失直接让前端重发
    images_ok, images_err = await _check_product_images_ready(task)
    if not images_ok:
        print(f"[restart] 🚫 task={task_id} 商品图不可用 → 拒绝重启: {images_err}", flush=True)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PRODUCT_IMAGES_MISSING",
                "message": "商品图片不可用，请重新上传商品图片后发起任务（原文件已丢失）",
                "missing": images_err,
            },
        )

    # 终态不能重启
    if task.phase in (TaskPhase.DONE.value, TaskPhase.FAILED.value):
        raise HTTPException(409, f"任务已 {task.phase}，无法重启")

    # graph 真在跑就拦（防并发）
    if _is_running(task_id):
        raise HTTPException(409, "任务正在执行中，不需要重启")

    # 读 checkpoint 看 graph 当前状态
    graph_state, snapshot = await _aget_graph_state(task_id)
    rt_state, rt_age = check_graph_runtime_state(snapshot)

    print(f"[restart] task={task_id} phase={task.phase} rt_state={rt_state} age={rt_age}",
          flush=True)

    if rt_state == "done":
        # graph 实际上已经跑完了，只是 DB phase 没同步？让前端重新 get 一次就行
        raise HTTPException(409, "任务已完成，无需重启")

    if rt_state == "paused":
        # graph 正停在 HITL，应该走 resume 而不是 restart
        raise HTTPException(409, "任务等待人工确认，请使用 resume 接口")

    # running / stale / none → 放行，让 graph.astream(None) 续跑
    # running 且年龄很小时说明 graph 真在跑，但 event_bus 里没标记 running
    # （比如后端刚重启），这种情况也放行——astream(None) 会从 checkpoint 继续

    # 注册 event_bus queue + 启动 graph
    from wellflow.app.event_bus import drain_and_subscribe
    q = await drain_and_subscribe(task_id)

    try:
        graph = _get_graph()
        config = _langgraph_config(task_id)
        # 关键：initial_state=None → 走 astream(None, config)
        # LangGraph 会直接从 checkpoint snapshot 续跑下一个节点
        asyncio.create_task(_start_graph(task_id, graph, config, initial_state=None))
    except Exception as exc:
        raise HTTPException(503, f"graph 不可用: {exc}")

    async def event_generator():
        yield _sse("restart_ack", {
            "task_id": task_id,
            "phase": task.phase,
            "rt_state": rt_state,
            "message": "已发起 graph 重启，从 checkpoint 续跑",
        })

        _HEARTBEAT_INTERVAL = 25
        while True:
            if await request.is_disconnected():
                print(f"[sse-restart] task={task_id} 前端断开", flush=True)
                break

            try:
                event = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_INTERVAL)
                event_type = event.get("type")
                event_data = event.get("data", {})

                yield _handle_sse_event(event_type, event_data)

                if event_type == "done" or event_type == "error":
                    break
                if event_type == "phase" and event_data.get("phase") == "failed":
                    break
                if event_type == "interrupt":
                    # graph 正常停下来等用户确认了，关闭 SSE
                    print(f"[sse-restart] task={task_id} interrupt(node={event_data.get('node')})，关闭 SSE", flush=True)
                    break
            except asyncio.TimeoutError:
                yield _sse("ping", {"ts": int(time.time())})

    return StreamingResponse(event_generator(), media_type="text/event-stream")


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
        print(f"[get_task] 从 checkpoint 读取 state 失败: {exc}", flush=True)
        return None, None


@router.get("/{task_id}", response_model=StandardResponse[TaskInfoResponse], summary="查询任务状态")
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

    # ── redo 等待态 early-exit ──────────────────────────────────────────────
    # checkpoint phase='waiting_selection' + interrupt.node='redo_selection'
    # 是 LangGraph aupdate_state 写的 redo 等待态。
    # resolve_current_node 只认 c1/c2/c3/c4 interrupt，会把 redo_selection 过滤掉，
    # 还会误把 snapshot.next 里的 cX interrupt 当成当前节点。
    # 这里在它之前直接用 checkpoint 里的值，跳过整个合成流程。
    if graph_state and graph_state.get("phase") == "waiting_selection":
        ckpt_interrupt = graph_state.get("interrupt")
        if isinstance(ckpt_interrupt, dict) and ckpt_interrupt.get("node") == "redo_selection":
            return ok(TaskInfoResponse(
                task_id=task.task_id,
                phase="waiting_selection",
                request=task.request_json or {},
                selected_plan_ids=task.selected_plan_ids_json or [],
                interrupt=ckpt_interrupt,
                cost=graph_state.get("cost", {}) or {},
                progress=graph_state.get("progress", {}) or {},
                node1=node1,
                node2=node2,
                node3=node3,
                model_images=[],
                output_images=[],
                created_at=to_cn_iso(task.created_at),
                updated_at=to_cn_iso(task.updated_at),
            ))

    # ── Step A: enrich node1.report_sections（历史 task 可能没有这个字段）───
    # 放在最前面——后面 graph_current_node 推出来的 c1 需要用它合成 interrupt
    if isinstance(node1, dict) and "report_sections" not in node1:
        from wellflow.app.prompt.report_sections import build_report_sections
        insight = node1.get("product_insight", "") or ""
        if insight:
            sections = build_report_sections(insight)
            node1 = {**node1, "report_sections": sections}

    # graph_context.resolve_current_node 重新从 checkpoint snapshot 推 current_node
    # —— DB 的 interrupt_json 可能被 _clear_interrupt 清掉（graph 每次跑前都会清旧 interrupt），
    # snapshot.next 才是唯一真相源。把推出来的 node 塞进 response.interrupt.node，
    # 前端 restoreConversation 靠这个判断 graph 停在哪。
    graph_current_node: str | None = None
    try:
        from wellflow.app.graph_context import resolve_current_node
        ctx = resolve_current_node(
            snapshot=snapshot,
            state=graph_state,
            db_interrupt_json=task.interrupt_json,
            db_phase=task.phase,
            verbose=False,
        )
        graph_current_node = ctx.current_node
        print(f"[get_task] task={task_id} resolved graph_current_node={graph_current_node} "
              f"confidence={ctx.confidence}", flush=True)
    except Exception as exc:
        print(f"[get_task] resolve_current_node 失败: {exc}", flush=True)

    # 合成 response.interrupt：
    #   - 优先用 DB 里的 interrupt_json（graph 最近停下来时写的，payload 完整）
    #   - DB 里没有就用 graph_current_node 合成一个轻量 interrupt（产物从 graph_state 里拿）
    interrupt_json = task.interrupt_json if isinstance(task.interrupt_json, dict) else None
    if interrupt_json is None and graph_current_node:
        node_payload_map = {
            "c1": {
                "node": "c1",
                "hint": "确认商品分析报告后继续",
                "report_sections": (node1 or {}).get("report_sections"),
                "product_insight": (node1 or {}).get("product_insight"),
                "thinking_text": (node1 or {}).get("thinking_text"),
            },
            "c2": {
                "node": "c2",
                "hint": "请选择商拍方案",
                "schemes": (node2 or {}).get("schemes"),
                "selected_scheme_indices": (node2 or {}).get("selected_scheme_indices"),
                "scheme_raw": (node2 or {}).get("scheme_raw"),
            },
            "c3": {
                "node": "c3",
                "hint": "请确认生图提示词",
                "generate_prompts": (node3 or {}).get("generate_prompts"),
                "prompts_detail": (node3 or {}).get("prompts_detail"),
            },
            "c4": {
                "node": "c4",
                "hint": "查看生图结果",
                "outputs": (node4 or {}).get("outputs"),
                "failed_items": (node4 or {}).get("failed_items"),
            },
        }
        interrupt_json = node_payload_map.get(graph_current_node)
        print(f"[get_task] DB interrupt_json 丢失，从 graph_state 合成 → node={graph_current_node}", flush=True)
    elif interrupt_json is not None and graph_current_node:
        # DB 里的 interrupt_json 有值但 graph_current_node 也推出来了——如果两者 node 不一致
        # （说明 DB 陈旧），就用 graph_current_node 覆盖 interrupt_json.node，
        # payload 保持 DB 的（更完整）。
        if interrupt_json.get("node") != graph_current_node:
            interrupt_json = {**interrupt_json, "node": graph_current_node}
            print(f"[get_task] DB interrupt_json.node={task.interrupt_json.get('node')} "
                  f"陈旧，覆盖为 graph_current_node={graph_current_node}", flush=True)

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
        interrupt=interrupt_json,
        cost=graph_state.get("cost", {}) if graph_state else {},
        progress=graph_state.get("progress", {}) if graph_state else {},
        node1=node1,
        node2=node2,
        node3=node3,
        model_images=model_images,
        output_images=output_images,
        created_at=to_cn_iso(task.created_at),
        updated_at=to_cn_iso(task.updated_at),
    ))


# ===========================================================================
# delete_task —— 彻底删除任务（DB + 磁盘文件 + LangGraph checkpoint）
# ===========================================================================


@router.delete("/{task_id}", response_model=StandardResponse[dict], summary="删除任务及所有关联数据")
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
