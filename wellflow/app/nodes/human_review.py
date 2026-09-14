"""Node 3：Human Review（final.md 第 7.4 节）。

批次审核的 interrupt 逻辑。图层面会在这里暂停，等用户 resume。
"""

from __future__ import annotations

from typing import Any


def prepare_batch_review(
    *,
    work_items: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """把当前批次的 work_items + 已生成图片 组装成供 UI 展示的数据。"""
    return {
        "work_items": work_items,
        "outputs": outputs,
        "batch_size": len(work_items),
        "hint": "请对每个 work_item 选择 approve / retry / reject，并可附带 feedback。",
    }


def apply_review_decisions(
    *,
    work_items: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    max_retry: int = 2,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """应用人工决定，返回 (要重试的 work_items, 最终通过的 work_items)。"""
    decisions_map = {d["work_item_id"]: d for d in decisions}
    retry: list[dict[str, Any]] = []
    approved: list[dict[str, Any]] = []

    for item in work_items:
        wid = item.get("work_item_id", "")
        dec = decisions_map.get(wid, {"decision": "approve", "feedback": ""})
        decision = dec.get("decision", "approve")

        if decision == "retry":
            retries = item.get("retry_count", 0)
            if retries >= max_retry:
                item["status"] = "rejected"
                approved.append(item)  # 超限的 reject 也推进
            else:
                item["status"] = "pending"
                item["retry_count"] = retries + 1
                item["feedback"] = dec.get("feedback", "")
                retry.append(item)
        elif decision == "reject":
            item["status"] = "rejected"
            approved.append(item)
        else:  # approve
            item["status"] = "done"
            approved.append(item)

    return retry, approved
