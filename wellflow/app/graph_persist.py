"""LangGraph 运行时的 DB 持久化辅助函数。

供 tasks.py（旧入口）和 chat.py（新 /api/chat 入口）共享：
两条路径都需要在 graph interrupt / phase 变化 / done 时同步写 DB，
否则 resume 接口读不到 interrupt_json 会报 409。
"""

from __future__ import annotations

from typing import Any

from wellflow.app.database import session_scope
from wellflow.app.repositories.task_repo import TaskRepo


def persist_phase(task_id: str, phase: str, node_name: str) -> None:
    from wellflow.app.models.task_models import Task, TaskEvent
    with session_scope() as db:
        task = db.get(Task, task_id)
        if task is None:
            raise RuntimeError("任务不存在")
        task.phase = phase
        task.interrupt_json = None
        db.add(TaskEvent(task_id=task_id, event_type="phase_change", phase=phase,
                         payload_json={"node": node_name}))
        db.commit()


def persist_interrupt(task_id: str, interrupt_value: dict[str, Any], phase: str = "") -> None:
    from wellflow.app.models.task_models import Task, TaskEvent
    from wellflow.app.workflow_status import canonical_interrupt
    payload = canonical_interrupt(interrupt_value)
    phase = payload["phase"]
    with session_scope() as db:
        task = db.get(Task, task_id)
        if task is None:
            raise RuntimeError("任务不存在")
        task.interrupt_json = payload
        task.phase = phase
        # All restore fields use the same payload as the actual checkpoint.
        db.add(TaskEvent(task_id=task_id, event_type=f"graph_interrupt_{payload['node']}",
                         phase=phase, payload_json=payload))
        db.commit()


def reconcile_checkpoint(task_id: str, snapshot) -> tuple[str, dict | None]:
    """Repair the business projection without fabricating checkpoint state."""
    from wellflow.app.models.task_models import Task, TaskEvent
    from wellflow.app.workflow_status import checkpoint_view
    phase, payload = checkpoint_view(snapshot)
    if phase == "missing":
        raise RuntimeError("任务 checkpoint 缺失，无法恢复，请重新创建任务")
    with session_scope() as db:
        task = db.get(Task, task_id)
        if task is None:
            raise RuntimeError("任务不存在")
        # Archival is a separate transaction whose durable receipt must finish.
        if task.phase == "done":
            return "done", None
        if task.phase == "archive_pending":
            return "archive_pending", payload
        changed = task.phase != phase or task.interrupt_json != payload
        task.phase = phase
        task.interrupt_json = payload
        state = snapshot.values
        if state.get("request"):
            task.request_json = state["request"]
        if changed and payload:
            db.add(TaskEvent(task_id=task_id, event_type=f"graph_interrupt_{payload['node']}",
                             phase=phase, payload_json=payload))
        db.commit()
    return phase, payload


def persist_error(task_id: str, code: str, message: str, source: str) -> None:
    from wellflow.app.models.task_models import Task, TaskErrorLog, TaskEvent
    with session_scope() as db:
        task = db.get(Task, task_id)
        if task is None:
            raise RuntimeError("任务不存在")
        if task.phase != "archive_pending":
            task.phase = "needs_retry"
            task.interrupt_json = None
        db.add(TaskEvent(task_id=task_id, event_type="graph_error", phase="needs_retry",
                         payload_json={"message": message, "retryable": True, "source": source}))
        db.add(TaskErrorLog(task_id=task_id, code=code, category="recoverable",
                            message=message, source=source, retryable=True))
        db.commit()


def persist_outputs(task_id: str, node4: dict[str, Any]) -> None:
    """确认结束（done）后：把 node4.outputs 的生图成品落盘 + 写 task_image 表。

    **同时往 task_event 追加一条 workflow_done 事件**，存本次生图的摘要
    （prompt 列表 + 图片 URL 列表），timeline API 能把完整的生图历史串起来。
    """
    outputs = node4.get("outputs") or []
    if not outputs:
        print(f"[graph] task={task_id} done 但 outputs 为空，跳过成品落库", flush=True)
        return

    from wellflow.app.utils.image_store import save_output_image

    images: list[dict[str, Any]] = []
    for o in outputs:
        wid = o.get("work_item_id") or f"shot-{int(o.get('prompt_index', 0)) + 1:02d}"
        storage_uri = save_output_image(task_id, wid, o.get("image_url") or "")
        images.append({
            "image_type": "output",
            "storage_uri": storage_uri,
            "shot_id": wid,
            "prompt": o.get("prompt"),
            "prompt_index": o.get("prompt_index"),
        })

    # 给 timeline 用的摘要（不重复存原始图片 URL——那在 task_image 表里有）
    summary_payload = {
        "output_count": len(outputs),
        "prompts": [o.get("prompt") for o in outputs if o.get("prompt")],
        "image_paths": [img["storage_uri"] for img in images],
        "phase": "done",
    }

    try:
        with session_scope() as db:
            repo = TaskRepo(db)
            ids = repo.save_images(task_id, images)
            # 追加 workflow_done 事件
            repo.add_event(
                task_id, "workflow_done",
                phase="done",
                payload_json=summary_payload,
            )
            print(f"[graph] task={task_id} 成品落库 {len(ids)} 张 + workflow_done 事件", flush=True)
    except Exception as e:
        print(f"[graph] output persist error: {e}", flush=True)
