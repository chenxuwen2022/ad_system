"""交付归档 finalize。

把 Node 4（generate_image）产物写回 state 并标记 phase=done。
DB 落盘在 API 层处理。
"""

from __future__ import annotations

from typing import Any


def finalize(state: dict[str, Any]) -> dict[str, Any]:
    """纯函数构造 delta 返回，不原地 mutate state。DB 落盘在 API 层处理。"""
    outputs = (state.get("node4") or {}).get("outputs", []) or []
    cost = dict(state.get("cost") or {})
    progress = dict(state.get("progress") or {})
    cost["final_image_count"] = len(outputs)
    progress["phase"] = "done"
    progress["completed"] = len(outputs)
    return {
        "phase": "done",
        "cost": cost,
        "progress": progress,
    }


# 向后兼容别名（parent_graph 用 finalize，API 层可能还有旧引用）
finalize_task = finalize

