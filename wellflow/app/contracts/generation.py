"""提示词与生图交付相关的结构化数据契约（final.md 第 7 节 + Node 3）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class GenerationWorkItem(BaseModel):
    """一个待执行的生图工作项。"""

    work_item_id: str
    plan_id: str
    shot_id: str
    skill: str
    prompt: str = ""
    prompt_file: str | None = None
    reference_images: list[str] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    status: Literal["pending", "running", "qaing", "human_review", "done", "rejected", "failed"] = "pending"


class QAResult(BaseModel):
    """AI QA 检测结果。"""

    passed: bool
    risk_level: Literal["low", "medium", "high"] = "low"
    issues: list[str] = Field(default_factory=list)
    correction_suggestion: str = ""
    manifest_ref: str | None = None
    round_index: int = 0


class GeneratedAsset(BaseModel):
    """一次生成的输出资产。"""

    asset_id: str
    work_item_id: str
    attempt_index: int = 0
    image_uri: str
    mime_type: str = "image/jpeg"
    provider: str | None = None
    model: str | None = None
    qa_result: QAResult | None = None
    prompt_snapshot: str = ""
    cost_estimate: float | None = None


class HumanReviewDecision(BaseModel):
    """对单个工作项的人工审核决定。"""

    work_item_id: str
    decision: Literal["approve", "retry", "reject"]
    feedback: str = ""


class HumanReviewState(BaseModel):
    """批次人工审核状态。"""

    batch_id: str
    work_items: list[HumanReviewDecision] = Field(default_factory=list)
    status: Literal["pending", "in_progress", "done"] = "pending"
