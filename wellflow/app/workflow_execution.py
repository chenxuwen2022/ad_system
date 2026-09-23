"""One execution owner per task, across HTTP entry points and server processes."""
from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager

from fastapi import HTTPException

from wellflow.app.event_bus import is_running, mark_running, mark_done, publish, cleanup, drain_and_subscribe
from wellflow.app.graph_persist import persist_phase, persist_error, reconcile_checkpoint
from wellflow.app.workflow_status import checkpoint_view


_background_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def execution_lease(task_id: str):
    # No await between the process-local test and claim.
    if is_running(task_id):
        raise HTTPException(409, "任务正在执行中，请等待完成")
    mark_running(task_id)
    conn = None
    try:
        from psycopg import AsyncConnection
        from wellflow.app.config import settings
        url = settings.database_url_async.replace("postgresql+asyncpg://", "postgresql://")
        conn = await AsyncConnection.connect(url, autocommit=True, connect_timeout=5)
        key = int.from_bytes(hashlib.sha256(task_id.encode()).digest()[:8], "big", signed=True)
        cursor = await conn.execute("SELECT pg_try_advisory_lock(%s)", (key,))
        row = await cursor.fetchone()
        if not row or not row[0]:
            raise HTTPException(409, "任务正在另一执行进程中运行，请等待完成")
        yield
    finally:
        if conn is not None:
            # Closing a dedicated session releases its advisory lock even on errors.
            await conn.close()
        mark_done(task_id)


async def _consume(task_id, graph, config, value):
    from wellflow.app.llm.model_pool import prepare_task_models
    try:
        await prepare_task_models(task_id)
        await asyncio.to_thread(persist_phase, task_id, "processing", "resume")
        # Defer actionable interrupt delivery until astream has finished writing
        # its checkpoint. Node progress events may still stream immediately.
        async for chunk in graph.astream(value, config=config, stream_mode="updates"):
            if not isinstance(chunk, dict):
                continue
            for name, delta in chunk.items():
                if name != "__interrupt__" and isinstance(delta, dict) and delta.get("phase"):
                    await asyncio.to_thread(persist_phase, task_id, delta["phase"], name)
        snapshot = await graph.aget_state(config)
        phase, interrupt = await asyncio.to_thread(reconcile_checkpoint, task_id, snapshot)
        # Returned to the caller to publish only after releasing the lease.
        return ("interrupt", interrupt) if interrupt else ("done", {"phase": phase})
    except Exception as exc:
        try:
            await asyncio.to_thread(persist_error, task_id, "GRAPH_RUNTIME_ERROR", str(exc), "workflow")
        except Exception:
            pass  # Preserve the original failure; checkpoint remains authoritative.
        return "error", {"phase": "needs_retry", "message": str(exc), "retryable": True}


async def launch_graph(task_id, graph, config, *, initial_state=None, command=None, queue=None):
    """Acquire before accepting a command. Background execution owns the lease."""
    lease = execution_lease(task_id)
    await lease.__aenter__()
    try:
        from wellflow.app.database import session_scope
        from wellflow.app.models.task_models import Task
        def db_phase():
            with session_scope() as db:
                task = db.get(Task, task_id)
                return task.phase if task else None
        phase_in_db = await asyncio.to_thread(db_phase)
        if phase_in_db is None:
            raise HTTPException(404, "任务不存在")
        if phase_in_db in ("archive_pending", "done"):
            raise HTTPException(409, "任务已入库或正在入库，请完成入库或创建新任务")
        snapshot = await graph.aget_state(config)
        phase, interrupt = checkpoint_view(snapshot)
        if initial_state is None:
            if phase == "missing":
                raise HTTPException(409, "任务 checkpoint 缺失，请重新创建任务")
            if phase == "done":
                raise HTTPException(409, "任务已完成，请创建新任务")
            if command is not None:
                values = command.resume
                if not interrupt or not isinstance(values, dict):
                    raise HTTPException(409, "当前没有可确认的暂停点，请恢复执行或刷新任务")
                request_update = (command.update or {}).get("request") or {}
                products = request_update.get("product_images")
                if products is not None and products != (snapshot.values.get("request") or {}).get("product_images"):
                    raise HTTPException(409, "商品图与已分析的报告不一致，请创建新任务更换商品")
                decision = values.get("decision", "confirm")
                allowed = {"c1": {"node1"}, "c2": {"node2"}, "c3": {"node2", "node3"}, "c4": {"node2", "node3"}}
                if decision == "refine" and values.get("refine_target", "node3" if interrupt["node"] == "c4" else "node" + interrupt["node"][-1]) not in allowed[interrupt["node"]]:
                    raise HTTPException(409, "当前阶段不允许修改该节点")
                if decision == "redo" and (interrupt["node"] != "c4" or values.get("redo_target", "node4") != "node4"):
                    raise HTTPException(409, "仅图片阶段支持重新生成")
                if decision not in ("confirm", "refine", "redo"):
                    raise HTTPException(400, "无效的确认操作")
                if decision == "confirm":
                    from wellflow.app.graph_context import validate_current_node_products
                    ok, missing = validate_current_node_products(interrupt["node"], snapshot.values)
                    if not ok:
                        raise HTTPException(409, "当前节点产物缺失，请重试该节点")
                if values.get("node", interrupt["node"]) != interrupt["node"]:
                    raise HTTPException(409, "任务节点已变化，请刷新后重试")
                if values.get("revision") and values["revision"] != interrupt.get("revision"):
                    raise HTTPException(409, "内容已更新，请刷新后重新确认")
            elif interrupt:
                # Idempotent recovery of a lost DB projection, without resuming.
                await asyncio.to_thread(reconcile_checkpoint, task_id, snapshot)
        q = queue if queue is not None else await drain_and_subscribe(task_id)
        value = command if command is not None else initial_state
        async def run():
            try:
                if initial_state is None and command is None and interrupt:
                    event = ("interrupt", interrupt)
                else:
                    event = await _consume(task_id, graph, config, value)
            finally:
                await lease.__aexit__(None, None, None)
            publish(task_id, event[0], event[1])
            cleanup(task_id)
        background = asyncio.create_task(run())
        _background_tasks.add(background)
        background.add_done_callback(_background_tasks.discard)
        return q
    except BaseException:
        await lease.__aexit__(None, None, None)
        raise
