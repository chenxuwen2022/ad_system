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
    """graph 节点跑完后更新 task.phase + 写 phase_change 事件。"""
    try:
        with session_scope() as db:
            repo = TaskRepo(db)
            repo.update_phase(task_id, phase)
            repo.add_event(task_id, "phase_change", phase=phase, payload_json={"node": node_name})
    except Exception:
        pass


def persist_interrupt(task_id: str, interrupt_value: dict[str, Any], phase: str) -> None:
    """graph 触发 interrupt 时存 interrupt_json + 更新 phase + 写 graph_interrupt 事件。"""
    try:
        with session_scope() as db:
            repo = TaskRepo(db)
            repo.save_interrupt(task_id, interrupt_value)
            repo.update_phase(task_id, phase)
            repo.add_event(
                task_id, "graph_interrupt",
                phase=phase,
                payload_json={"node": interrupt_value.get("node")},
            )
    except Exception as e:
        print(f"[graph] interrupt persist error: {e}", flush=True)


def persist_error(task_id: str, code: str, message: str, source: str) -> None:
    try:
        from wellflow.app.models.task_models import TaskPhase

        with session_scope() as db:
            repo = TaskRepo(db)
            repo.add_error(task_id, code, "unrecoverable", message, source=source)
            repo.update_phase(task_id, TaskPhase.FAILED.value)
    except Exception:
        pass


def persist_outputs(task_id: str, node4: dict[str, Any]) -> None:
    """确认结束（done）后：把 node4.outputs 的生图成品落盘 + 写 task_image 表。"""
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
            "variant_index": o.get("variant_index"),
        })

    try:
        with session_scope() as db:
            repo = TaskRepo(db)
            ids = repo.save_images(task_id, images)
            print(f"[graph] task={task_id} 成品落库 {len(ids)} 张", flush=True)
    except Exception as e:
        print(f"[graph] output persist error: {e}", flush=True)
