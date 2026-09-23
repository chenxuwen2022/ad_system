"""Checkpoint-backed operational state. Product presence is diagnostic only.

Only recorded checkpoint interrupts authorize a decision. DB phase and historic
products cannot manufacture a pause or declare a task complete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wellflow.app.config import settings


# ---------------------------------------------------------------------------
# LangGraph 节点名常量（与 parent_graph.py 严格对齐）
# ---------------------------------------------------------------------------

# HITL interrupt 节点（graph 会停在这里等用户操作）
INTERRUPT_NODE_TO_C: dict[str, str] = {
    "c1_confirm_report": "c1",
    "c2_select_scheme": "c2",
    "c3_confirm_prompt": "c3",
    "c4_review_result": "c4",
}

# 执行中节点（graph 正在跑，但跑完就会跳到下一个 interrupt）
# key 是执行节点名 → value 是它后面的那个 cX（如果执行完就会停在那里）
EXEC_NODE_NEXT_INTERRUPT: dict[str, str] = {
    "node1_product_analyzer": "c1",       # 跑完 Node1 → 停 C1
    "node2_planning_scheme": "c2",        # 跑完 Node2 → 停 C2
    "node3_prompt_generation": "c3",      # 跑完 Node3 → 停 C3
    "node4_generate_image": "c4",         # 跑完 Node4 → 停 C4_review
    "finalize": "",                        # finalize 完了 → done
}

# phase → cX 映射（state["phase"] / DB phase 列通用）
PHASE_TO_C: dict[str, str] = {
    "c1_confirm": "c1",
    "c2_select": "c2",
    "c3_confirm": "c3",
    "c4_review": "c4",
}

# DB phase 列（历史遗留命名）
DB_PHASE_TO_C: dict[str, str] = {
    **PHASE_TO_C,
    "c2_confirm": "c2",      # 旧名兼容
    "c4_generating": "c4",   # 不精确，但在 c4 阶段
}

# confirmations key → cX 名
CONFIRMATION_KEYS = ("c1", "c2", "c3", "c4")


# ---------------------------------------------------------------------------
# graph 运行状态检测
# ---------------------------------------------------------------------------


_GRAPH_STATE_INTERRUPT = "paused"          # graph 暂停在 interrupt 上，等用户操作 → 正常 dispatch
_GRAPH_STATE_RUNNING_FRESH = "running"     # 执行节点 + checkpoint 很新 → 真在跑
_GRAPH_STATE_RUNNING_STALE = "stale"       # 执行节点 + checkpoint 很旧 → 可能挂了
_GRAPH_STATE_NEVER_STARTED = "none"        # checkpoint 不存在

# checkpoint 年龄阈值统一在 config.py（graph_stale_threshold_seconds）


def _parse_iso_time(value: str | None):
    """解析 ISO 8601 字符串为 timezone-aware datetime；失败返回 None。"""
    if not value:
        return None
    from datetime import datetime, timezone
    try:
        # Python 3.11+ fromisoformat 支持 timezone；3.12 原生 OK
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def check_graph_runtime_state(snapshot: Any) -> tuple[str, float | None]:
    """graph 运行时状态：返回 (状态枚举, checkpoint_年龄秒数 or None)。

    Returns:
        "paused"         — snapshot.next 有 interrupt 节点
        "running"        — snapshot.next 全执行节点 + checkpoint 年龄 < stale_threshold
        "stale"          — snapshot.next 全执行节点 + checkpoint 年龄 >= stale_threshold
        "none"           — snapshot 为 None（checkpoint 不存在）
        "done"           — snapshot.next 为空列表（graph 已到 END，finalize 完成）
    """
    from wellflow.app.workflow_status import checkpoint_view
    phase, interrupt = checkpoint_view(snapshot)
    if phase == "missing":
        return "none", None
    if interrupt:
        return "paused", None
    if phase == "done":
        return "done", None
    if phase == "needs_retry":
        return "stale", None
    from datetime import datetime, timezone
    parsed = _parse_iso_time(getattr(snapshot, "created_at", None))
    age = None
    if parsed:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - parsed).total_seconds()
    return ("stale" if age is None or age >= settings.graph_stale_threshold_seconds else "running"), age


def is_graph_paused(snapshot: Any) -> bool:
    """向后兼容：graph 是否真的暂停在某个 interrupt 上。"""
    state, _ = check_graph_runtime_state(snapshot)
    return state == _GRAPH_STATE_INTERRUPT


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class GraphContext:
    """多源校验后的上下文。"""
    current_node: str | None          # "c1" / "c2" / "c3" / "c4" / None
    confidence: str                    # "snapshot_next" | "snapshot_interrupts" | "state_phase" | "state_confirmations" | "db_interrupt" | "db_phase" | "unknown"
    source_next: list[str] = field(default_factory=list)
    source_state_phase: str | None = None
    source_confirmations: dict[str, bool] = field(default_factory=dict)
    source_db_interrupt_node: str | None = None
    source_db_phase: str | None = None
    conflicts: list[str] = field(default_factory=list)  # 高优 vs 低优冲突日志


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------


def resolve_current_node(
    *,
    snapshot: Any = None,
    state: dict[str, Any] | None = None,
    db_interrupt_json: dict[str, Any] | None = None,
    db_phase: str | None = None,
    task_phase: str | None = None,  # 别名
    verbose: bool = True,
) -> GraphContext:
    """Resolve an actionable node from the checkpoint's actual interrupts."""
    from wellflow.app.workflow_status import checkpoint_view
    phase, interrupt = checkpoint_view(snapshot)
    ctx = GraphContext(
        current_node=interrupt.get("node") if interrupt else None,
        confidence="checkpoint_interrupt" if interrupt else phase,
        source_next=list(getattr(snapshot, "next", ()) or ()),
        source_state_phase=(state or {}).get("phase"),
        source_db_phase=task_phase or db_phase,
        source_db_interrupt_node=(db_interrupt_json or {}).get("node"),
    )
    if verbose:
        _log_ctx(ctx)
    return ctx


# ---------------------------------------------------------------------------
# 从 graph_state 各 Node 的产物反推"已经走到哪一步了"
# ---------------------------------------------------------------------------

# 每个 Node 判定"已跑完"的信号字段（至少命中一个）
_NODE_PRODUCT_KEYS: dict[str, tuple[str, ...]] = {
    "node1": ("product_insight",),                        # 有报告 → Node1 已出
    "node2": ("schemes",),                                # 有方案列表 → Node2 已出
    "node3": ("generate_prompts", "prompts_detail"),      # 有 prompt 列表 → Node3 已出
    "node4": ("outputs",),                                # 有生图产物 → Node4 已出
}

# node 索引 → cX 编号
_NODE_INDEX_TO_C: dict[int, str] = {1: "c1", 2: "c2", 3: "c3", 4: "c4"}


def infer_current_node_from_state(state: dict[str, Any] | None) -> str | None:
    """Diagnostic stage hint only; never used to authorize resume/restart."""
    if not isinstance(state, dict):
        return None

    last_completed_idx = 0
    for i in range(1, 5):
        node = state.get(f"node{i}", {}) or {}
        if not isinstance(node, dict):
            continue
        keys = _NODE_PRODUCT_KEYS.get(f"node{i}", ())
        # 命中任一非空信号就算"已跑完"
        hit = any(
            bool(node.get(k))
            for k in keys
        )
        if hit:
            last_completed_idx = i

    if last_completed_idx == 0:
        return None

    # Node N produces the content reviewed at C N.
    next_idx = last_completed_idx
    if next_idx > 4:
        next_idx = 4
    return _NODE_INDEX_TO_C.get(next_idx)


# ---------------------------------------------------------------------------
# 硬性规则：校验当前 cX 所需的上游 nodeX 产物是否完整
# ---------------------------------------------------------------------------

# cX 依赖 nodeX 哪些字段（非 None 且非空列表/空 dict）
_C_NODE_PRODUCT_REQUIREMENTS: dict[str, list[tuple[str, ...]]] = {
    "c1": [("node1", "product_insight")],  # node1 已跑完，product_insight 必须存在
    "c2": [("node2", "schemes")],  # c2 依赖 node2.schemes 非空列表
    "c3": [("node3", "generate_prompts"), ("node3", "prompts_detail")],  # 至少命中一个
    "c4": [("node4", "work_items")],  # node4.outputs 可能为空（全部生图失败），但 work_items 必须在
}


def validate_current_node_products(
    current_node: str | None,
    state: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    """硬性守卫：校验当前 HITL 节点 cX 所需的上游产物是否完整。

    规则：绝不允许跳过损坏 / 数据缺失的节点。
      实例：current_node=c2，但 state.node2.schemes=[] → 阻断 confirm 向下流转，
      只允许 retry/redo 当前 node2。

    Returns:
        (ok, missing_fields) —— ok=True 表示产物完整；ok=False 时 missing_fields 列出缺什么。
        当 current_node 为 None 或不在已知列表里时返回 (True, []) —— 调用方应该先校验 current_node。
    """
    if not isinstance(state, dict) or not state:
        return False, ["checkpoint"]
    if not current_node:
        return False, ["interrupt"]

    if current_node not in _C_NODE_PRODUCT_REQUIREMENTS:
        return True, []

    missing: list[str] = []
    reqs = _C_NODE_PRODUCT_REQUIREMENTS[current_node]

    # 每个 requirement 是一个 tuple of (node_key, field_key)
    # 特殊处理：c3 的 generate_prompts / prompts_detail 二选一即可
    if current_node == "c3":
        n3 = state.get("node3", {}) or {}
        has_any = bool(n3.get("generate_prompts"))
        if not has_any:
            missing.append("node3.generate_prompts / node3.prompts_detail")
    else:
        for node_key, field_key in reqs:
            node = state.get(node_key, {}) or {}
            val = node.get(field_key)
            if not val:
                missing.append(f"{node_key}.{field_key}")

    ok = len(missing) == 0
    if not ok:
        print(
            f"[validate] 🛡️ current_node={current_node} 产物缺失 → missing={missing}",
            flush=True,
        )
    return ok, missing


# ---------------------------------------------------------------------------
# 归一化工具
# ---------------------------------------------------------------------------


def _normalize_confirmations(state: dict[str, Any] | None) -> dict[str, bool]:
    """state["confirmations"] 可能是 TaskState 里的 dict_reducer 合并结构，做安全归一化。"""
    out: dict[str, bool] = {}
    if not state:
        return out
    conf = state.get("confirmations")
    if isinstance(conf, dict):
        for key in CONFIRMATION_KEYS:
            val = conf.get(key)
            out[key] = bool(val) if val is not None else False
    return out


def _log_ctx(ctx: GraphContext) -> None:
    """打印解析过程 —— 用于排查 current_node 丢失。"""
    from time import time
    print(f"[graph-ctx] resolved current_node={ctx.current_node!r} "
          f"confidence={ctx.confidence}", flush=True)
    if ctx.source_next:
        print(f"  snapshot.next = {ctx.source_next!r}", flush=True)
    if ctx.source_state_phase:
        print(f"  state.phase = {ctx.source_state_phase!r}", flush=True)
    if ctx.source_confirmations:
        print(f"  confirmations = {ctx.source_confirmations!r}", flush=True)
    if ctx.source_db_interrupt_node:
        print(f"  db.interrupt_json.node = {ctx.source_db_interrupt_node!r}", flush=True)
    if ctx.source_db_phase:
        print(f"  db.phase = {ctx.source_db_phase!r}", flush=True)
    if ctx.conflicts:
        for c in ctx.conflicts:
            print(f"  ⚠️ conflict: {c}", flush=True)
