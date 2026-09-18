"""从 LangGraph checkpoint / DB 多源反推 current_node（c1 ~ c4）。

这是 **单一真相源**：所有意图分类、dispatch、守卫校验都应该从这里拿 current_node，
不再自己手写 DB 查询 / phase 反推等分散逻辑。

优先级（由高到低）：
  1. snapshot.next 里的 interrupt 节点名 → 100% 准确（LangGraph 停下就是因为 interrupt）
  2. snapshot.tasks[i].interrupts 里的 interrupt 对象（与 next 等价）
  3. state["phase"] —— LangGraph 写入的 phase 字段（不是 DB 的 phase 列！）
  4. state["confirmations"] —— 哪些 cX 已经确认（兜底推断"最后通过的 cX 就是当前阶段"）
  5. DB interrupt_json["node"] —— DB 层持久化的 interrupt 信息
  6. DB phase 列 —— 仅覆盖少量 *_confirm 阶段，兜底用

冲突检测：如果高优先级源和低优先级源不一致，会打印 WARN 但仍取高优先级。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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

# checkpoint 年龄阈值：超过这个秒数还没进入下一个 interrupt，认为 graph 可能挂了
# Node2 VLM 生成 3 套方案 → 典型耗时 30-60 秒，给 90 秒 buffer 足够
_STALE_THRESHOLD_SECONDS = 90


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
    if snapshot is None or not hasattr(snapshot, "next"):
        return _GRAPH_STATE_NEVER_STARTED, None

    # snapshot.next 有 interrupt 节点 → 暂停
    try:
        next_nodes = snapshot.next or []
    except Exception:
        next_nodes = []

    if any(str(n) in INTERRUPT_NODE_TO_C for n in next_nodes):
        return _GRAPH_STATE_INTERRUPT, None

    # snapshot.next 为空列表 → graph 已到 END（finalize 完成）
    if not next_nodes:
        return "done", None

    # 全是执行节点 → 看 checkpoint 年龄
    created_at = getattr(snapshot, "created_at", None)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    age_seconds: float | None = None
    parsed = None
    if isinstance(created_at, str):
        parsed = _parse_iso_time(created_at)
    if parsed is not None:
        # 确保 aware
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age_seconds = (now - parsed).total_seconds()

    if age_seconds is None or age_seconds < _STALE_THRESHOLD_SECONDS:
        return _GRAPH_STATE_RUNNING_FRESH, age_seconds
    return _GRAPH_STATE_RUNNING_STALE, age_seconds


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
    """多源校验求 current_node。

    参数可以都不传（例如新任务），也可以只传某几个。函数会自动跳过 None 的源。

    Usage (chat.py 推荐写法)::

        snapshot = await g.aget_state(config)
        graph_state = snapshot.values if snapshot and hasattr(snapshot, "values") else None
        ctx = resolve_current_node(
            snapshot=snapshot,
            state=graph_state,
            db_interrupt_json=task.interrupt_json,
            db_phase=task.phase,
        )
        current_node = ctx.current_node
    """
    ctx = GraphContext(
        current_node=None,
        confidence="unknown",
        source_db_phase=task_phase or db_phase,
        source_db_interrupt_node=(db_interrupt_json or {}).get("node") if db_interrupt_json else None,
        source_state_phase=(state or {}).get("phase") if state else None,
    )
    ctx.source_confirmations = _normalize_confirmations(state)

    # --------- 1. snapshot.next (最优先) ---------
    next_nodes: list[str] = []
    if snapshot is not None and hasattr(snapshot, "next"):
        try:
            raw = snapshot.next
            next_nodes = [str(n) for n in (raw or [])]
        except Exception:
            next_nodes = []
    ctx.source_next = next_nodes

    for n in next_nodes:
        if n in INTERRUPT_NODE_TO_C:
            ctx.current_node = INTERRUPT_NODE_TO_C[n]
            ctx.confidence = "snapshot_next"
            break

    # --------- 2. snapshot.tasks[].interrupts (等价于 next，备用) ---------
    if ctx.current_node is None and snapshot is not None and hasattr(snapshot, "tasks"):
        try:
            tasks = snapshot.tasks or []
            for t in tasks:
                interrupts = getattr(t, "interrupts", None) or []
                for intr in interrupts:
                    val = getattr(intr, "value", None)
                    if isinstance(val, dict):
                        node = val.get("node")
                        if node in ("c1", "c2", "c3", "c4"):
                            ctx.current_node = node
                            ctx.confidence = "snapshot_interrupts"
                            break
                if ctx.current_node:
                    break
        except Exception:
            pass

    # --------- 3. confirmations 兜底（比 state.phase 更可信）---------
    # state.phase 可能残留上一个 interrupt 的值（执行中节点还没写入新 phase）。
    # confirmations 是 LangGraph state reducer 合并出来的，只会被 True 覆盖，不会被残留。
    if ctx.current_node is None:
        last_confirmed = None
        for key in CONFIRMATION_KEYS:
            if ctx.source_confirmations.get(key):
                last_confirmed = key
        if last_confirmed is not None:
            # 最后确认的是 cX → graph 已经过了 cX，正在跑 NodeX+1 或停在 cX+1 interrupt
            x = int(last_confirmed[1])  # "c2" → 2
            next_x = x + 1
            if next_x <= 4:
                ctx.current_node = f"c{next_x}"
                ctx.confidence = "state_confirmations"

    # --------- 4. state["phase"] (LangGraph 写入的) ---------
    if ctx.current_node is None and ctx.source_state_phase:
        c = PHASE_TO_C.get(ctx.source_state_phase)
        if c:
            ctx.current_node = c
            ctx.confidence = "state_phase"

    # --------- 5. DB interrupt_json ---------
    if ctx.current_node is None and ctx.source_db_interrupt_node:
        if ctx.source_db_interrupt_node in ("c1", "c2", "c3", "c4"):
            ctx.current_node = ctx.source_db_interrupt_node
            ctx.confidence = "db_interrupt"

    # --------- 6. DB phase 列 ---------
    if ctx.current_node is None and ctx.source_db_phase:
        c = DB_PHASE_TO_C.get(ctx.source_db_phase)
        if c:
            ctx.current_node = c
            ctx.confidence = "db_phase"

    # --------- 7. 新任务兜底 ---------
    # 如果 confirmations 全 False，且没有 snapshot interrupt，说明 graph 刚启动还没跑到 C1。
    # 前提：至少有一个源（checkpoint snapshot.next 或 DB 信息）存在——否则可能根本没任务，不该猜 c1。
    _has_any_source = bool(ctx.source_next or ctx.source_db_phase or ctx.source_db_interrupt_node)
    if ctx.current_node is None and _has_any_source and not any(ctx.source_confirmations.values()):
        # 再确认 snapshot.next 里也没有任何已到过的 interrupt（如果有说明状态异常）
        if not any(n in INTERRUPT_NODE_TO_C for n in ctx.source_next):
            ctx.current_node = "c1"
            ctx.confidence = "new_task_default"

    # --------- 8. done / failed 状态排除 ---------
    # graph 已结束时（snapshot.next 为空），"严格意义上"没有 interrupt 节点了。
    #
    # 关键：只有 snapshot 本身才能证明 graph 已结束。db.phase='failed' / 'done'
    # 可能是 DB 里的陈旧值（比如 graph 后来又 resume 到了 cX，但 DB 忘了更），
    # 绝不能用它来覆盖 snapshot.next 推出来的 valid interrupt。
    # state.phase 同理——只有 snapshot 确认没有 interrupt 了才清。
    if ctx.current_node and not any(n in INTERRUPT_NODE_TO_C for n in ctx.source_next):
        # snapshot.next 里确实没有 interrupt 节点 → 再看 phase 是不是真的 terminal
        if ctx.source_state_phase in ("done", "failed") or ctx.source_db_phase in ("done", "failed"):
            ctx.current_node = None
            ctx.confidence = "terminal_state"

    # --------- 9. 产物兜底推断（最后抓手）---------
    # 只有 snapshot.next 里也没有任何 interrupt 节点时才跑这个兜底。
    # 如果 snapshot.next 里有 'cX_select_*' 这样的 interrupt，
    # 说明 graph 真的停在那，直接用 step 1 推出来的 current_node 就好，
    # 别让产物兜底把它盖掉——产物兜底是"graph 已经跑过去又回来了"
    # 这种场景的修复手段，不是正常路径的 primary 来源。
    _snapshot_has_interrupt = any(n in INTERRUPT_NODE_TO_C for n in ctx.source_next)
    inferred: str | None = None
    if ctx.current_node is None and not _snapshot_has_interrupt:
        inferred = infer_current_node_from_state(state)
        if inferred:
            ctx.current_node = inferred
            ctx.confidence = "state_products"
            if verbose:
                print(f"  ↳ 产物推断兜底 → current_node={inferred}", flush=True)

    # --------- 冲突检测 ---------
    if ctx.current_node and ctx.current_node is not None:
        expected = ctx.current_node
        for src_name, candidate in [
            ("db_interrupt", ctx.source_db_interrupt_node),
            ("db_phase", DB_PHASE_TO_C.get(ctx.source_db_phase or "", "") or None),
            ("state_phase", PHASE_TO_C.get(ctx.source_state_phase or "", "") or None),
        ]:
            if candidate and candidate != expected:
                ctx.conflicts.append(f"[{src_name}]={candidate!r} vs resolved={expected!r}")

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
    """从 graph_state 的 Node 产物反推"下一个要停的 interrupt 节点"。

    规则：遍历 node1→node4，找到**最后一个有产物**的 node；
      - 若它是 nodeN，则已完成 N；下一个要停的 interrupt 是 c{N+1}。
      - 若没有任何产物 → None（任务尚未开始）。
      - 若 state 是 None → None。

    这个推断的意义：
      snapshot.next / DB interrupt_json 可能因为清理时序丢失，
      但 nodeX 产物还在 checkpoint 里。用户输入"重做"时
      靠这个函数能反推出应该弹哪个阶段的 redo 选项。

    注意：这只在 graph_state 确实存在时才靠谱；纯新任务（state=None）
    不应该被误判成 c1。
    """
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
            (node.get(k) not in (None, [], {}))
            for k in keys
        )
        if hit:
            last_completed_idx = i

    if last_completed_idx == 0:
        return None

    # 已跑完 last_completed_idx → 下一个 interrupt 是 c{N+1}
    # 特例：跑完 node4 但 phase 还是 c4_review（还没 finalize）→ c4
    # finalize 之后（phase=done）用户也会说"重做" → 需要走到 c4 让用户选
    next_idx = last_completed_idx + 1
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
    if not current_node or not isinstance(state, dict):
        return True, []

    if current_node not in _C_NODE_PRODUCT_REQUIREMENTS:
        return True, []

    missing: list[str] = []
    reqs = _C_NODE_PRODUCT_REQUIREMENTS[current_node]

    # 每个 requirement 是一个 tuple of (node_key, field_key)
    # 特殊处理：c3 的 generate_prompts / prompts_detail 二选一即可
    if current_node == "c3":
        n3 = state.get("node3", {}) or {}
        has_any = bool(
            (n3.get("generate_prompts") not in (None, [], {}))
            or (n3.get("prompts_detail") not in (None, [], {}))
        )
        if not has_any:
            missing.append("node3.generate_prompts / node3.prompts_detail")
    else:
        for node_key, field_key in reqs:
            node = state.get(node_key, {}) or {}
            val = node.get(field_key)
            if val in (None, [], {}):
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
