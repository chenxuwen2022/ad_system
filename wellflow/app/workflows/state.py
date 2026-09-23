"""LangGraph 状态模型。

重构后的 4 Node + 4 HITL 流水线：
  Node1 (product_analyzer)   → C1 →
  Node2 (planning_scheme)    → C2 (选方案) →
  Node3 (prompt_generation)  → C3 (确认提示词) →
  Node4 (generate_image)     → C4 (重做/确认) → finalize

各 Node 有自己的子状态。

⚠️ LangGraph 1.2.x 的 LastValue channel 默认不允许同 superstep 多写入。
   Command(update=X, goto=Y) 这种原子提交如果 update 和目标 node return 都写了同一个
   channel（比如 phase、node3），就会 InvalidUpdateError。
   解决方案：顶层 state 字段全部用 merge_state_values 自定义 reducer，
   dict 类型做字段级 merge，其他类型后来者覆盖。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict


def _merge_state_values(a: Any, b: Any) -> Any:
    """LangGraph reducer —— dict 用 | 字段级合并，其他类型后来者覆盖。

    签名必须是 (a, b) -> c，符合 LangGraph Annotated[type, reducer] 约定。
    a = 当前值（channel 已有内容），b = 本次 node Command/update 提交的新值。

    ⚠️ 字段级 merge 的副作用：写 `nodeX: {}` 是 no-op（旧键全保留），
    无法用于 redo/jump 的"清空节点"场景。清理路径请用 cleared() 生成
    带 __clear__ 哨兵的写入值，这里识别后会整体替换（丢弃旧 dict）。
    """
    if isinstance(b, dict) and b.get(_CLEAR_FLAG) is True:
        return {k: v for k, v in b.items() if k != _CLEAR_FLAG}
    if isinstance(a, dict) and isinstance(b, dict):
        return a | b  # Python 3.9+ dict merge
    return b  # 标量 / list / None → 后来者覆盖


_CLEAR_FLAG = "__clear__"


def cleared(**keep: Any) -> dict[str, Any]:
    """生成"整体替换"语义的 dict 写入值：清空该字段后仅保留 keep 中的键。

    用于 redo / graph jump 时清空下游节点（如 node2/3/4），避免
    _merge_state_values 的字段级 merge 把 {} 当 no-op 导致旧数据残留。
    """
    return {_CLEAR_FLAG: True, **keep}


# LangGraph reducer 签名：(prev, new) -> merged
# 把函数暴露成模块级常量，方便 Annotated 引用
REDUCER = _merge_state_values


def _normalize_refine_history(raw: Any) -> dict[str, list[str]]:
    """兼容老 checkpoint 的历史格式归一化 helper。

    老任务的 checkpoint 里 _refine_history 可能还是旧的 flat list[str]。
    统一归一成 dict[node_name, list[str]]，方便后续按 node 隔离读写。

    旧 list 的处理策略：**不做 node 归属猜测，把它丢弃**——
    因为 flat list 无法判断每条指令属于哪个 node，硬塞反而会继续污染新逻辑。
    （老任务续跑时，后续 refine 会正常按 node 写入，历史指令只丢失旧的 flat 部分，可接受）
    """
    if isinstance(raw, dict):
        return {k: list(v) for k, v in raw.items() if isinstance(v, list)}
    # 老格式：list[str] → 丢弃（无法判断归属哪个 node）
    return {}


def get_node_refine_history(state: Any, node_name: str) -> list[str]:
    """从 state 里取某一 node 的 refine 历史（带旧格式兼容）。

    用法：
        from wellflow.app.workflows.state import get_node_refine_history
        h = get_node_refine_history(state, "node1")  # list[str]
    """
    if not isinstance(state, dict):
        return []
    raw = state.get("_refine_history")
    normalized = _normalize_refine_history(raw)
    return list(normalized.get(node_name, []))


def build_refine_history_update(
    prev_history: Any, node_name: str, new_instruction: str
) -> dict[str, list[str]]:
    """构造 `_refine_history` 的写入值：只更新指定 node 的历史，其他 node 保持原样。

    用法（parent_graph / chat.py 的 refine 入口）：
        from wellflow.app.workflows.state import build_refine_history_update
        new_val = build_refine_history_update(prev, "node1", "补品牌调性")
        # 返回值形如 {"node1": ["旧1", "旧2", "补品牌调性"]}
        # 全局 REDUCER 对 dict 做 a|b 键级 merge，会自动保留 node2/node3 旧历史
    """
    normalized = _normalize_refine_history(prev_history)
    node_list = list(normalized.get(node_name, []))
    if new_instruction:
        node_list.append(new_instruction)
    return {node_name: node_list}


class Progress(TypedDict, total=False):
    phase: str
    total_work_items: int
    completed: int
    qaring: int
    human_review: int


class CostSummary(TypedDict, total=False):
    estimated_min: float
    estimated_max: float
    accumulated: float


class InterruptSnapshot(TypedDict, total=False):
    node: Literal["c1", "c2", "c3", "c4"]
    schema: dict[str, Any]
    hint: str


class TaskError(TypedDict, total=False):
    code: str
    category: str
    source: str
    message: str
    retryable: bool


# ---------------------------------------------------------------------------
# Node1：ProductAnalyzer — VLM 商品识别
# ---------------------------------------------------------------------------


class Node1State(TypedDict, total=False):
    input_analysis: dict[str, Any]
    product_insight: str             # VLM 输出的 Markdown 报告全文
    report_sections: dict[str, Any]   # 归一化的四块结构（前端"重点洞察"面板使用）
    next_actions: str                 # LLM 动态生成的下一步引导语（报告正文 ---NEXT--- 分隔线之下的部分）
    compressed_images: list[str]      # 商品图 data URI 缓存（给下游复用）
    thinking_text: str                # VLM 深度思考过程文本（用于刷新后恢复展示）
    # —— 确认/锁定相关字段（由 C1 confirm 写入，锁定后任何入口都不得修改）——
    report_locked: bool               # True = 用户已确认并锁定；当前任务内不可再改
    report_hash: str                  # 当次确认时 product_insight 的哈希（版本绑定）
    confirmed_at: float               # 当次确认的 unix 时间戳
    # —— 多轮修改计数（调试用，可选）——
    refine_count: int


# ---------------------------------------------------------------------------
# Node2：PlanningScheme — VLM 生成多套自由文本商拍方案，C2 选定一套
# ---------------------------------------------------------------------------


class SchemeState(TypedDict, total=False):
    schemes: list[dict[str, Any]]          # 多套方案载体，每套包含 report_text
    scheme_raw: str                         # VLM 原始报告文本
    selected_scheme_indices: list[int]      # C2 用户选定的方案索引（新链路通常只有 1 套被锁）
    per_scheme_count: list[int]             # 前端为每套选中方案指定的 prompt 数量
    thinking_text: str                      # VLM 深度思考过程文本


# ---------------------------------------------------------------------------
# Node3：PromptGeneration — 为每套选中方案调一次 VLM，生成最终生图 prompt
# ---------------------------------------------------------------------------


class PromptState(TypedDict, total=False):
    # 三类结构化参考图（C2 阶段用户上传，可选；空列表=没传）
    reference_images: dict[Literal["mannequin", "scene", "outfit"], list[str]]
    ratio: str                              # C1 用户选的画面比例
    image_model: str                        # 生图模型选择

    # 由 Node3 产出（循环 N 次，N = sum(node2.per_scheme_count)）
    generate_prompts: list[str]             # N 份最终 prompt 字符串（每份 = 一张生图）
    prompts_detail: list[dict[str, Any]]    # 对应每个 prompt 的详情：{scheme_index, variant_index, prompt_raw, ...}
    prompt_raw: str                         # VLM 原始输出拼接（调试用）

    # C3 interrupt 后 resume 写入
    per_prompt_size: list[str]              # 每个 prompt 的图片规格，如 ["3:4", "3:4"]
    thinking_text: str                      # VLM 深度思考过程文本（多个方案的 thinking 拼接）


# ---------------------------------------------------------------------------
# Node4：GenerateImage — 批量并发调 LLM 生图
# ---------------------------------------------------------------------------


class Node4State(TypedDict, total=False):
    reference_images: list[str]             # 商品图+模特图文件路径列表
    reference_images_data_uris: list[str]   # 一次性缓存的 data URI（避免重复 PIL）
    work_items: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    failed_items: list[dict[str, Any]]
    retry_failed_only: bool
    generation_status: str
    generation_summary: str
    requested_count: int
    completed_count: int
    human_review: dict[str, Any]


# ---------------------------------------------------------------------------
# 顶层 TaskState
# ---------------------------------------------------------------------------


class TaskState(TypedDict, total=False):
    # —— 以下字段全部用 _merge_state_values reducer ——
    # LangGraph 1.2.x 默认 LastValue channel 在同 superstep 多次写入时直接炸。
    # 我们所有 Command(update=..., goto=...) 原子提交场景（backward / resume）
    # 都会 update phase/nodeX/request 等字段，而目标 node 自己又会 return 这些字段，
    # 所以同 step 多写入是**必然**会发生的，不能依赖"不会撞"。
    # reducer 策略：dict 做字段级 | merge，标量/list 后来者覆盖（符合"更新"语义）。
    task_id: Annotated[str, REDUCER]
    phase: Annotated[str, REDUCER]
    request: Annotated[dict[str, Any], REDUCER]
    brand_config: Annotated[dict[str, Any], REDUCER]
    node1: Annotated[Node1State, REDUCER]
    node2: Annotated[SchemeState, REDUCER]
    node3: Annotated[PromptState, REDUCER]
    node4: Annotated[Node4State, REDUCER]
    # HITL 确认记录：{"c1": True, ...}。用户点"生成方案"确认 c1 时由 _c1_confirm_report 写入，
    # 供 compute_completed_mask 反推已完成步骤（interrupt 丢失后仍能识别 c1 已确认）
    confirmations: Annotated[dict[str, bool], REDUCER]
    progress: Annotated[Progress, REDUCER]
    cost: Annotated[CostSummary, REDUCER]
    interrupt: Annotated[InterruptSnapshot | None, REDUCER]
    error: Annotated[TaskError | None, REDUCER]
    event_ids: Annotated[list[str], REDUCER]

    # ---- 临时控制字段（refine / redo 专用，消费后自动清空）----
    # refine 路径：interrupt resume(decision="refine") 写入，refine 节点消费后清 None
    _refine_target: Annotated[str | None, REDUCER]       # "node1" | "node2" | "node3" | None
    _refine_instruction: Annotated[str | None, REDUCER]  # 用户修改指令文本
    # node2 refine 专用：LLM 意图分类器返回的 selected_indices
    # 决定 refine_node2_schemes 能看到哪几套原方案（用户明确点名了哪些 → 只传那些；"all"或None → 全部传）
    _refine_selected_indices: Annotated[list[int] | str | None, REDUCER]
    # 多轮 refine 历史（按 node 隔离，避免 node1/node2/node3 指令互相污染）
    # 旧格式兼容：如果 checkpoint 里还是 list（老任务），_get_node_refine_history helper 会自动 wrap 成 {'nodeX': list}
    # refine 节点用它做指令整合（处理"用户前一轮让你补品牌调性，这一轮品牌名已明确 → 自动去重"）
    _refine_history: Annotated[dict[str, list[str]], REDUCER]  # {node1|node2|node3: [instruction,...]}，含本轮
    # redo 路径：仅 C4 redo→node4 保留（其他节点都走 refine）
    _redo_target: Annotated[str | None, REDUCER]         # "node4" | None
