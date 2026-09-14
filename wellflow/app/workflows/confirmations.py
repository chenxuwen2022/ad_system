"""C1 / C2 interrupt 逻辑 + 字段 diff 复用（final.md 第 5.6 / 6.4 节）。"""

from __future__ import annotations

from typing import Any


# 这些字段修改后需要重跑 ProductAnalyzer → ResearchAgent
_RESEARCH_TRIGGER_FIELDS = {
    "category", "material", "style", "functional_features",
}

# 这些字段只影响 PlanningAgent，不需要回到 Node 1
_PLANNING_TRIGGER_FIELDS = {
    "direction", "asset_types", "concept", "tone",
}


def c1_recompute_decision(
    original_profile: dict[str, Any],
    confirmed_profile: dict[str, Any],
    platform_changed: bool = False,
    marketing_goal_changed: bool = False,
) -> dict[str, Any]:
    """C1 diff：判断哪些部分需要重跑。

    返回：
      {
        "rerun_research": bool,
        "rerun_analyzer": bool,
        "reuse_insight": bool,
      }
    """
    rerun_research = platform_changed or marketing_goal_changed

    for field in _RESEARCH_TRIGGER_FIELDS:
        orig_val = _extract_value(original_profile.get(field))
        new_val = _extract_value(confirmed_profile.get(field))
        if orig_val != new_val:
            rerun_research = True
            break

    return {
        "rerun_research": rerun_research,
        "rerun_analyzer": False,  # C1 阶段 ProductAnalyzer 已完成且输入未变
        "reuse_insight": not rerun_research,
    }


def c2_recompute_decision(
    original_plan: dict[str, Any],
    override: dict[str, Any],
) -> dict[str, Any]:
    """C2 diff。"""
    rerun_planning = any(
        k in override for k in _PLANNING_TRIGGER_FIELDS
    ) or not override.get("shot_overrides")

    return {
        "rerun_planning": rerun_planning,
        "rerun_binding": bool(override.get("shot_overrides")),
        "reuse_plans": not rerun_planning,
    }


def _extract_value(field: Any) -> Any:
    """EvidenceField 可能是 {"value": ..., "evidence": ...} 也可能是纯值。"""
    if isinstance(field, dict):
        return field.get("value")
    return field
