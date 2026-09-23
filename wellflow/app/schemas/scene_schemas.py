# -*- coding: utf-8 -*-
"""场景库 API 请求/响应模型(仿 outfit_schemas)。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SceneDims(BaseModel):
    """五维场景标签(每维可多选)。"""

    space: list[str] = Field(default_factory=list)       # 空间类型
    region: list[str] = Field(default_factory=list)      # 地域环境
    season: list[str] = Field(default_factory=list)      # 季节
    weather: list[str] = Field(default_factory=list)     # 天气
    sceneStyle: list[str] = Field(default_factory=list)  # 背景风格


class SceneExtractRequest(BaseModel):
    """场景 AI 处理请求:点击即入库 extracting,后台提取+打标(马赛克原图)。

    scene_id 传了=对已有待确认行「换图重处理」(不新建)。
    """

    original_uri: str
    session_id: str | None = None
    scene_id: int | None = None


class SceneUpdateRequest(BaseModel):
    """编辑/确认入库/重新整理请求。

    status 取值:
      "active"     = 确认入库(仅待确认行;直接可用,无审核环节)
      "extracting" = 重新整理(仅待确认行;清空处理产物,后台重跑 AI)
    """

    name: str | None = None
    desc: str | None = None
    tags: str | None = None
    cover_storage_uri: str | None = None
    original_storage_uri: str | None = None
    dims: SceneDims | None = None
    status: Literal["extracting", "active"] | None = None


class SceneListItem(BaseModel):
    id: int
    scene_no: str
    name: str
    desc: str | None = None
    tags: str | None = None
    scope: str = "mine"
    origin: str | None = None
    status: str = "extracting"   # extracting/pending_confirm/active/failed
    cover_storage_uri: str | None = None
    cover_url: str | None = None
    original_storage_uri: str | None = None
    original_url: str | None = None
    mosaic_storage_uri: str | None = None
    mosaic_url: str | None = None
    created_at: str = ""
    updated_at: str = ""


class SceneDetailResponse(BaseModel):
    id: int
    scene_no: str
    name: str
    desc: str | None = None
    tags: str | None = None
    scope: str = "mine"
    origin: str | None = None
    status: str = "extracting"
    cover_storage_uri: str | None = None
    cover_url: str | None = None
    original_storage_uri: str | None = None
    original_url: str | None = None
    mosaic_storage_uri: str | None = None
    mosaic_url: str | None = None
    dims: dict[str, list[str]] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class SceneListResponse(BaseModel):
    items: list[SceneListItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
