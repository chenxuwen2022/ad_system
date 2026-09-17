"""交付归档 finalize。

把 Node 4（generate_image）产物写回 state 并标记 phase=done。
DB 落盘在 API 层处理。
"""

from __future__ import annotations

from typing import Any


def finalize(state: dict[str, Any]) -> dict[str, Any]:
    """纯函数更新 state；DB 落盘在 API 层处理。"""
    state["phase"] = "done"
    node4 = state.get("node4", {})
    outputs = node4.get("outputs", [])

    # 简单汇总
    state.setdefault("cost", {})
    accumulated = state["cost"].get("accumulated", 0.0)
    state["cost"]["accumulated"] = accumulated
    state["cost"]["final_image_count"] = len(outputs)

    state.setdefault("progress", {})
    state["progress"]["phase"] = "done"
    state["progress"]["completed"] = len(outputs)

    return state


# 向后兼容别名（parent_graph 用 finalize，API 层可能还有旧引用）
finalize_task = finalize

