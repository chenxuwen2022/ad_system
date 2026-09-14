"""API 请求/响应的 Pydantic schema（任务域）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 创建任务
# ---------------------------------------------------------------------------

class TaskCreateRequest(BaseModel):
    description: str = ""
    platform: Literal["taobao", "jd", "douyin", "tiktok-shop"] = "taobao"
    image_type: Literal["ad", "listing", "social"] = "ad"
    marketing_goal: Literal["acquisition", "retention"] = "acquisition"
    product_link: str | None = None
    image_model: str | None = None
    brand_config: dict[str, Any] = Field(default_factory=dict)


class TaskCreateResponse(BaseModel):
    task_id: str
    phase: str
    estimated_cost_range: list[float] = Field(default_factory=list)
    interrupt: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# 查询 / 恢复
# ---------------------------------------------------------------------------

class TaskResumeRequest(BaseModel):
    node: Literal["c1", "c2", "review"]
    values: dict[str, Any]


class TaskInfoResponse(BaseModel):
    task_id: str
    phase: str
    request: dict[str, Any]
    selected_plan_ids: list[str] = Field(default_factory=list)
    interrupt: dict[str, Any] | None = None
    cost: dict[str, Any] = Field(default_factory=dict)
    progress: dict[str, Any] = Field(default_factory=dict)
    # Node 输出（从 LangGraph checkpoint 读取，可能为 None）
    node1: dict[str, Any] | None = None
    node2: dict[str, Any] | None = None
    node3: dict[str, Any] | None = None
    # 任务关联图片（从 task_image 表读出）
    model_images: list[dict[str, Any]] = Field(default_factory=list)  # C1 上传的模特图
    output_images: list[dict[str, Any]] = Field(default_factory=list)  # 确认结束后的生图成品
    created_at: str = ""
    updated_at: str = ""


class TaskListItem(BaseModel):
    task_id: str
    phase: str
    platform: str = ""
    marketing_goal: str = ""
    description: str = ""
    has_interrupt: bool = False
    created_at: str = ""
    updated_at: str = ""


class TaskListResponse(BaseModel):
    items: list[TaskListItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------

class SSEEvent(BaseModel):
    event: Literal["phase", "progress", "interrupt", "cost", "done", "error", "ping"]
    data: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Provider 白名单
# ---------------------------------------------------------------------------

class ProviderInfo(BaseModel):
    name: str
    models: list[str] = Field(default_factory=list)
    notes: str = ""


class ProvidersResponse(BaseModel):
    providers: list[ProviderInfo] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
