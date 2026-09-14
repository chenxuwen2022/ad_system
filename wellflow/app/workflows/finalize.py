"""交付归档 finalize（final.md 第 2 / 10 节）。

把 Node 3 产物写回 task 表并标记 phase=done。
"""

from __future__ import annotations

from typing import Any


def finalize_task(state: dict[str, Any], task_repo=None) -> dict[str, Any]:
    """纯函数更新 state；DB 落盘在 API 层处理。"""
    state["phase"] = "done"
    node3 = state.get("node3", {})
    outputs = node3.get("outputs", [])

    # 简单汇总
    state.setdefault("cost", {})
    accumulated = state["cost"].get("accumulated", 0.0)
    state["cost"]["accumulated"] = accumulated
    state["cost"]["final_image_count"] = len(outputs)

    state.setdefault("progress", {})
    state["progress"]["phase"] = "done"
    state["progress"]["completed"] = len(outputs)

    return state
