"""模特库 API（参考素材 > 模特库）。"""

from __future__ import annotations

import json as json_mod
from typing import Any

from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.orm import Session

from wellflow.app.database import get_db
from wellflow.app.repositories.mannequin_repo import MannequinRepo, MANNEQUIN_DIMENSION_GROUPS
from wellflow.app.schemas.asset_schemas import (
    MannequinCreateRequest, MannequinUpdateRequest,
    MannequinListItem, MannequinListResponse,
    MannequinDetailResponse, MannequinDimensionsResponse,
    MannequinTagIn,
)


router = APIRouter(prefix="/reference/mannequins", tags=["模特库"])


def _storage_uri_url(uri: str | None) -> str | None:
    if not uri:
        return None
    if uri.startswith("http"):
        return uri
    return "/" + uri.lstrip("/")


def _tags_to_grouped(tags_rows) -> list[MannequinTagIn]:
    """把 repo 返回的扁平 tag 列表按 group_key+dim_key 聚合。"""
    grouped: dict[tuple[str, str], list[str]] = {}
    for t in tags_rows:
        key = (t["group_key"], t["dim_key"])
        grouped.setdefault(key, []).append(t["tag_value"])
    result = []
    for (gk, dk), vals in grouped.items():
        result.append(MannequinTagIn(group_key=gk, dim_key=dk, tag_values=vals))
    return result


# ============================================================================
# 维度枚举（前端下拉菜单）
# ============================================================================

@router.get("/dimensions", response_model=MannequinDimensionsResponse, summary="获取全部维度选项（前端筛选下拉菜单用）")
def get_dimensions():
    """返回硬编码的维度分组和可选值。"""
    return MannequinDimensionsResponse(groups=MANNEQUIN_DIMENSION_GROUPS)


# ============================================================================
# CRUD
# ============================================================================

@router.get("", response_model=MannequinListResponse, summary="列出模特（支持 scope / q 搜索 / 多维筛选）")
def list_mannequins(
    scope: str | None = Query(default=None, description="official / mine / 全部(不传)"),
    q: str | None = Query(default=None, description="关键词或自然语言描述"),
    dims: str | None = Query(default=None, description="多维筛选 JSON: {\"性别\":[\"女\"],\"模特风格\":[\"极简\",\"高级\"]}"),
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
):
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 100:
        page_size = 20

    parsed_dims: dict[str, list[str]] | None = None
    if dims:
        try:
            raw = json_mod.loads(dims)
            parsed_dims = {k: [str(x) for x in v] for k, v in raw.items() if v}
        except Exception:
            raise HTTPException(400, "dims 参数必须是合法 JSON")

    repo = MannequinRepo(db)
    items, total = repo.list(scope=scope, q=q, dims=parsed_dims, page=page, page_size=page_size)

    out_items = []
    for m in items:
        tag_rows = repo.list_tags(m.id)
        tag_summary = []
        for t in tag_rows:
            tag_summary.extend(t["tag_values"])
        # 前 5 个标签作为卡片展示
        out_items.append(MannequinListItem(
            id=m.id,
            mannequin_no=m.mannequin_no,
            name=m.name,
            en_name=m.en_name,
            scope=m.scope,
            origin=m.origin,
            status=m.status,
            cover_storage_uri=m.cover_storage_uri,
            tag_summary=tag_summary[:5],
            description=m.description,
            created_at=m.created_at.isoformat(),
            updated_at=m.updated_at.isoformat(),
        ))

    return MannequinListResponse(items=out_items, total=total, page=page, page_size=page_size)


@router.get("/{mannequin_id}", response_model=MannequinDetailResponse, summary="查询模特详情")
def get_mannequin(mannequin_id: int, db: Session = Depends(get_db)):
    repo = MannequinRepo(db)
    m = repo.get(mannequin_id)
    if not m:
        raise HTTPException(404, "模特不存在")
    tag_rows = repo.list_tags(mannequin_id)
    return MannequinDetailResponse(
        id=m.id,
        mannequin_no=m.mannequin_no,
        name=m.name,
        en_name=m.en_name,
        scope=m.scope,
        origin=m.origin,
        status=m.status,
        cover_storage_uri=m.cover_storage_uri,
        cover_url=_storage_uri_url(m.cover_storage_uri),
        description=m.description,
        generate_model=m.generate_model,
        generate_prompt=m.generate_prompt,
        tags=_tags_to_grouped(tag_rows),
        created_at=m.created_at.isoformat(),
        updated_at=m.updated_at.isoformat(),
    )


@router.post("", response_model=MannequinDetailResponse, summary="创建模特（上传方式）")
def create_mannequin(body: MannequinCreateRequest, db: Session = Depends(get_db)):
    """创建模特资产。上传方式：cover_storage_uri 传图；AI 生成方式：同时传 AI 相关字段。"""
    repo = MannequinRepo(db)
    try:
        m = repo.create(
            name=body.name,
            en_name=body.en_name,
            scope=body.scope,
            origin=body.origin,
            cover_storage_uri=body.cover_storage_uri,
            description=body.description,
            tags=[t.model_dump() for t in body.tags] if body.tags else None,
            # AI 生成字段
            input_desc=body.input_desc,
            input_refs=body.input_refs,
            final_prompt=body.final_prompt,
            generate_model=body.generate_model,
            num_output=body.num_output,
        )
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(400, f"创建失败: {e}")

    tag_rows = repo.list_tags(m.id)
    return MannequinDetailResponse(
        id=m.id, mannequin_no=m.mannequin_no, name=m.name, en_name=m.en_name,
        scope=m.scope, origin=m.origin, status=m.status,
        cover_storage_uri=m.cover_storage_uri,
        cover_url=_storage_uri_url(m.cover_storage_uri),
        description=m.description,
        generate_model=m.generate_model, generate_prompt=m.generate_prompt,
        tags=_tags_to_grouped(tag_rows),
        created_at=m.created_at.isoformat(), updated_at=m.updated_at.isoformat(),
    )


@router.put("/{mannequin_id}", response_model=MannequinDetailResponse, summary="更新模特")
def update_mannequin(mannequin_id: int, body: MannequinUpdateRequest, db: Session = Depends(get_db)):
    repo = MannequinRepo(db)
    try:
        m = repo.update(
            mannequin_id,
            name=body.name,
            en_name=body.en_name,
            scope=body.scope,
            cover_storage_uri=body.cover_storage_uri,
            description=body.description,
            tags=[t.model_dump() for t in body.tags] if body.tags is not None else None,
        )
        db.commit()
    except ValueError as e:
        raise HTTPException(404, str(e))

    tag_rows = repo.list_tags(m.id)
    return MannequinDetailResponse(
        id=m.id, mannequin_no=m.mannequin_no, name=m.name, en_name=m.en_name,
        scope=m.scope, origin=m.origin, status=m.status,
        cover_storage_uri=m.cover_storage_uri,
        cover_url=_storage_uri_url(m.cover_storage_uri),
        description=m.description,
        generate_model=m.generate_model, generate_prompt=m.generate_prompt,
        tags=_tags_to_grouped(tag_rows),
        created_at=m.created_at.isoformat(), updated_at=m.updated_at.isoformat(),
    )


@router.delete("/{mannequin_id}", summary="删除模特（级联清理标签，生成日志保留）")
def delete_mannequin(mannequin_id: int, db: Session = Depends(get_db)):
    repo = MannequinRepo(db)
    ok = repo.delete(mannequin_id)
    if not ok:
        raise HTTPException(404, "模特不存在")
    db.commit()
    return {"deleted": True, "mannequin_id": mannequin_id}
