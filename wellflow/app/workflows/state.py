"""LangGraph 状态模型。

重构后的 4 Node + 4 HITL 流水线：
  Node1 (product_analyzer)   → C1 →
  Node2 (planning_scheme)    → C2 (选方案) →
  Node3 (prompt_generation)  → C3 (确认提示词) →
  Node4 (generate_image)     → C4 (重做/确认) → finalize

各 Node 有自己的子状态。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


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
    compressed_images: list[str]      # 商品图 data URI 缓存（给下游复用）


# ---------------------------------------------------------------------------
# Node2：PlanningScheme — VLM 生成 N 套结构化商拍方案（12 维 JSON）
# ---------------------------------------------------------------------------


class SchemeState(TypedDict, total=False):
    schemes: list[dict[str, Any]]          # 3 套完整 12 维 JSON（每套 = PLANNING_AGENT_SYSTEM_PROMPT 输出）
    scheme_raw: str                         # VLM 原始 JSON 文本（前端展示/调试）
    selected_scheme_indices: list[int]      # C2 选的方案索引，如 [0, 2] 或 [0, 1, 2]


# ---------------------------------------------------------------------------
# Node3：PromptGeneration — 为每套选中方案调一次 VLM，生成最终生图 prompt
# ---------------------------------------------------------------------------


class PromptState(TypedDict, total=False):
    model_images: list[str]                 # C1 用户上传的模特图文件路径（可选）
    ratio: str                              # C1 用户选的画面比例
    image_model: str                        # 生图模型选择

    # 由 Node3 产出（循环 N 次，N = 选中方案数）
    generate_prompts: list[str]             # 每套选中方案 → 1 个最终 prompt 字符串
    prompts_detail: list[dict[str, Any]]    # 对应每个 prompt 的详情：{scheme_index, prompt_raw, ...}
    prompt_raw: str                         # VLM 原始输出拼接（调试用）

    # C3 interrupt 后 resume 写入
    per_prompt_count: list[int]             # 每个 prompt 生成几张图，如 [3, 1]
    per_prompt_size: list[str]              # 每个 prompt 的图片规格，如 ["3:4", "3:4"]
    compressed_model_images: list[str]      # 模特图 data URI 缓存


# ---------------------------------------------------------------------------
# Node4：GenerateImage — 批量并发调 LLM 生图
# ---------------------------------------------------------------------------


class Node4State(TypedDict, total=False):
    reference_images: list[str]             # 商品图+模特图文件路径列表
    reference_images_data_uris: list[str]   # 一次性缓存的 data URI（避免重复 PIL）
    work_items: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    failed_items: list[dict[str, Any]]
    human_review: dict[str, Any]


# ---------------------------------------------------------------------------
# 顶层 TaskState
# ---------------------------------------------------------------------------


class TaskState(TypedDict, total=False):
    task_id: str
    phase: str                     # input → analyze → c1_confirm → plan_scheme → c2_select →
                                    #   gen_prompt → c3_confirm → generate → c4_review → done / failed
    request: dict[str, Any]
    brand_config: dict[str, Any]
    node1: Node1State
    node2: SchemeState
    node3: PromptState
    node4: Node4State
    progress: Progress
    cost: CostSummary
    interrupt: InterruptSnapshot | None
    error: TaskError | None
    event_ids: list[str]
