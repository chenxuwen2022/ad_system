"""LangGraph 状态模型（final.md 第 4 节）。

TaskState 是父图状态，各 Node 有自己的子状态。
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
    node: Literal["c1", "c2", "review"]
    schema: dict[str, Any]
    hint: str


class TaskError(TypedDict, total=False):
    code: str
    category: str
    source: str
    message: str
    retryable: bool


# ---------------------------------------------------------------------------
# 各 Node 子状态
# ---------------------------------------------------------------------------


class Node1State(TypedDict, total=False):
    input_analysis: dict[str, Any]
    prepared_images: list[dict[str, Any]]
    product_profile: dict[str, Any]
    research_insight: dict[str, Any]
    product_insight: dict[str, Any]
    confirmation: dict[str, Any]


class Node2State(TypedDict, total=False):
    model_images: list[str]          # 用户上传的模特图文件路径列表（如 uploads/taskX/m0.jpg）
    ratio: str                        # 画面比例，如 "9:16竖版"
    count: int                        # 生成屏数（1-10）
    image_model: str                  # 用户选的图像生成模型（前端下拉框传，覆盖 config 默认）
    generate_prompts: list[str]       # VLM 返回的 JSON 数组（Node 3 直接用）
    planning_result: str             # VLM 原始 JSON 文本（前端展示用）
    planning_brief: dict[str, Any]   # 旧版规划数据（暂保留，未被新流程使用）
    asset_candidates: dict[str, Any] # 旧版资产库查询结果（暂保留，未被新流程使用）
    plans: list[dict[str, Any]]      # 旧版方案列表（暂保留，未被新流程使用）
    confirmation: dict[str, Any]


class Node3State(TypedDict, total=False):
    reference_images: list[str]         # 商品图+模特图文件路径列表（LLM 调用前转 data URI，interrupt 给前端时也转）
    work_items: list[dict[str, Any]]
    current_work_item_id: str | None
    outputs: list[dict[str, Any]]
    failed_items: list[dict[str, Any]]  # 生图失败明细：{work_item_id, prompt_index, variant_index, prompt, error}
    human_review: dict[str, Any]


# ---------------------------------------------------------------------------
# 顶层 TaskState
# ---------------------------------------------------------------------------


class TaskState(TypedDict, total=False):
    task_id: str
    phase: str                     # input / research / c1_confirm / planning / c2_confirm / delivery / done / failed
    request: dict[str, Any]
    brand_config: dict[str, Any]
    node1: Node1State
    node2: Node2State
    node3: Node3State
    selected_plan_ids: list[str]
    progress: Progress
    cost: CostSummary
    interrupt: InterruptSnapshot | None
    error: TaskError | None
    event_ids: list[str]
