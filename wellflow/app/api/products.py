"""SKU 商品库 API（品牌 / 系列 / SKU）。

核心约束：
  - brand / series 不在此处创建（由 SKU 创建接口自动 find-or-create）
  - 但 brand / series 有独立的 GET / PUT / DELETE（用于维护和清理）
  - DELETE brand/series 使用 RESTRICT，有子对象时拒绝
  - DELETE sku 只级联清自己的子表，不动上层
"""

from __future__ import annotations

import json as json_mod
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.orm import Session

from wellflow.app.database import get_db
from wellflow.app.api.utils import ok, StandardResponse
from wellflow.app.repositories.product_repo import BrandRepo, SeriesRepo, SkuRepo
from wellflow.app.schemas.asset_schemas import (
    BrandResponse, BrandUpdateRequest,
    SeriesResponse, SeriesUpdateRequest,
    SkuCreateRequest, SkuUpdateRequest,
    SkuListResponse, SkuListItem,
    SkuDetailResponse, SkuImageResponse,
    NavNode,
)


router = APIRouter(prefix="/products", tags=["SKU 商品库"])


def _storage_uri_url(uri: str | None) -> str:
    if not uri:
        return ""
    if uri.startswith("http"):
        return uri
    return "/" + uri.lstrip("/")


# ============================================================================
# 导航树
# ============================================================================

@router.get("/nav", summary="左侧品牌/系列/SKU 导航树", response_model=StandardResponse[list[dict]])
def get_nav(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    brand_repo = BrandRepo(db)
    series_repo = SeriesRepo(db)
    sku_repo = SkuRepo(db)

    brands = brand_repo.list()
    result: list[dict[str, Any]] = []
    for b in brands:
        series_list = series_repo.list_by_brand(b.id)
        brand_node: dict[str, Any] = {
            "type": "brand", "id": b.id, "label": b.name,
            "count": b.series_count + b.sku_count,
            "children": [],
        }
        for s in series_list:
            skus, _ = sku_repo.list(series_id=s.id, page_size=1000)
            series_node: dict[str, Any] = {
                "type": "series", "id": s.id, "label": s.name,
                "count": s.sku_count,
                "children": [
                    {"type": "sku", "id": sk.id, "label": sk.name, "count": 0}
                    for sk in skus
                ],
            }
            brand_node["children"].append(series_node)
        result.append(brand_node)
    return ok(result)


# ============================================================================
# Brand
# ============================================================================

@router.get("/brands", response_model=StandardResponse[list[BrandResponse]], summary="列出所有品牌")
def list_brands(db: Session = Depends(get_db)):
    repo = BrandRepo(db)
    return ok([_brand_to_dict(b) for b in repo.list()])


@router.get("/brands/{brand_id}", response_model=StandardResponse[BrandResponse], summary="查询单个品牌")
def get_brand(brand_id: int, db: Session = Depends(get_db)):
    repo = BrandRepo(db)
    brand = repo.get(brand_id)
    if not brand:
        raise HTTPException(404, "品牌不存在")
    return ok(_brand_to_dict(brand))


@router.put("/brands/{brand_id}", response_model=StandardResponse[BrandResponse], summary="更新品牌信息")
def update_brand(brand_id: int, body: BrandUpdateRequest, db: Session = Depends(get_db)):
    repo = BrandRepo(db)
    try:
        brand = repo.update(brand_id, **body.model_dump(exclude_none=True))
        db.commit()
    except ValueError as e:
        raise HTTPException(404, str(e))
    return ok(_brand_to_dict(brand))


@router.delete("/brands/{brand_id}", response_model=StandardResponse[dict], summary="删除品牌（有系列时拒绝）")
def delete_brand(brand_id: int, db: Session = Depends(get_db)):
    repo = BrandRepo(db)
    ok_repo, msg = repo.delete(brand_id)
    if not ok_repo:
        raise HTTPException(409, msg)
    db.commit()
    return ok({"deleted": True, "brand_id": brand_id})


# ============================================================================
# Series
# ============================================================================

@router.get("/series", response_model=StandardResponse[list[SeriesResponse]], summary="列出系列（可按 brand_id 过滤）")
def list_series(
    brand_id: int | None = Query(None, description="按品牌 ID 过滤，不传则返回全部系列"),
    db: Session = Depends(get_db),
):
    from wellflow.app.models.asset_models import ProductSeries
    repo = SeriesRepo(db)
    if brand_id:
        items = repo.list_by_brand(brand_id)
    else:
        items = list(db.query(ProductSeries).all())
    return ok([_series_to_dict(s) for s in items])


@router.get("/series/{series_id}", response_model=StandardResponse[SeriesResponse], summary="查询单个系列")
def get_series(series_id: int, db: Session = Depends(get_db)):
    repo = SeriesRepo(db)
    series = repo.get(series_id)
    if not series:
        raise HTTPException(404, "系列不存在")
    return ok(_series_to_dict(series))


@router.put("/series/{series_id}", response_model=StandardResponse[SeriesResponse], summary="更新系列（可迁移到其他品牌）")
def update_series(series_id: int, body: SeriesUpdateRequest, db: Session = Depends(get_db)):
    repo = SeriesRepo(db)
    try:
        series = repo.update(series_id, **body.model_dump(exclude_none=True))
        db.commit()
    except ValueError as e:
        raise HTTPException(404, str(e))
    return ok(_series_to_dict(series))


@router.delete("/series/{series_id}", response_model=StandardResponse[dict], summary="删除系列（有 SKU 时拒绝）")
def delete_series(series_id: int, db: Session = Depends(get_db)):
    repo = SeriesRepo(db)
    ok_repo, msg = repo.delete(series_id)
    if not ok_repo:
        raise HTTPException(409, msg)
    db.commit()
    return ok({"deleted": True, "series_id": series_id})


# ============================================================================
# SKU
# ============================================================================

@router.get("/skus", response_model=StandardResponse[SkuListResponse], summary="列出 SKU（分页 + 搜索 + 过滤）")
def list_skus(
    search: str | None = Query(None, description="按 SKU 名称/货号/SKU 编号模糊搜索"),
    brand_id: int | None = Query(None, description="按品牌 ID 精确过滤"),
    series_id: int | None = Query(None, description="按系列 ID 精确过滤"),
    page: int = Query(1, description="当前页码，从 1 开始"),
    page_size: int = Query(20, description="每页条数，默认 20"),
    db: Session = Depends(get_db),
):
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 100:
        page_size = 20
    repo = SkuRepo(db)
    items, total = repo.list(search=search, brand_id=brand_id, series_id=series_id,
                             page=page, page_size=page_size)
    return ok(SkuListResponse(
        items=[_sku_to_list_item(s, db) for s in items],
        total=total, page=page, page_size=page_size,
    ))


@router.get("/skus/{sku_id}", response_model=StandardResponse[SkuDetailResponse], summary="查询 SKU 详情")
def get_sku(sku_id: int, db: Session = Depends(get_db)):
    repo = SkuRepo(db)
    sku = repo.get(sku_id)
    if not sku:
        raise HTTPException(404, "SKU 不存在")
    return ok(_sku_to_detail(sku, db))


@router.post("/skus", response_model=StandardResponse[SkuDetailResponse], summary="创建 SKU（品牌/系列自动 find-or-create）")
def create_sku(body: SkuCreateRequest, db: Session = Depends(get_db)):
    brand_repo = BrandRepo(db)
    series_repo = SeriesRepo(db)
    sku_repo = SkuRepo(db)

    # 1. brand：id 优先，否则 name 自动 find-or-create
    if body.brand_id:
        brand = brand_repo.get(body.brand_id)
        if not brand:
            raise HTTPException(400, f"brand_id={body.brand_id} 不存在")
        if body.brand_guide:
            brand.brand_guide = body.brand_guide
    elif body.brand_name:
        brand = brand_repo.get_or_create(body.brand_name)
        if body.brand_guide:
            brand.brand_guide = body.brand_guide
    else:
        raise HTTPException(400, "必须提供 brand_id 或 brand_name 之一")

    # 2. series：id 优先，否则 name 自动 find-or-create
    if body.series_id:
        series = series_repo.get(body.series_id)
        if not series:
            raise HTTPException(400, f"series_id={body.series_id} 不存在")
        if series.brand_id != brand.id:
            raise HTTPException(400, "指定的 series 不属于指定的 brand")
    elif body.series_name:
        series = series_repo.get_or_create(brand.id, body.series_name)
    else:
        raise HTTPException(400, "必须提供 series_id 或 series_name 之一")

    # 3. 查重（series + style_no + name 三元组）
    existing = sku_repo.find_duplicate(series.id, body.style_no, body.name)
    if existing:
        db.rollback()  # 清理 brand/series get_or_create 的 flush 副作用
        raise HTTPException(409, detail={
            "message": "该商品已存在",
            "sku_id": existing.id,
            "sku_no": existing.sku_no,
            "name": existing.name,
            "style_no": existing.style_no,
        })

    # 4. 创建 SKU
    from sqlalchemy.exc import IntegrityError
    try:
        sku = sku_repo.create(
            series_id=series.id,
            brand_id=brand.id,
            name=body.name,
            style_no=body.style_no,
            category=body.category,
            color=body.color,
            material=body.material,
            silhouette=body.silhouette,
            season=body.season,
            selling_points=body.selling_points,
            brand_summary=body.brand_summary or brand.brand_guide,
            source_url=body.source_url,
            images=[m.model_dump() for m in body.images],
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        # 并发竞争下 find_duplicate 没查到，但唯一约束拒绝了——重新查返回完整信息
        dup = sku_repo.find_duplicate(series.id, body.style_no, body.name)
        detail = {
            "message": "该商品已存在",
            "sku_id": dup.id if dup else None,
            "sku_no": dup.sku_no if dup else None,
            "name": dup.name if dup else body.name,
            "style_no": dup.style_no if dup else body.style_no,
        }
        raise HTTPException(409, detail=detail)
    except Exception as e:
        db.rollback()
        raise HTTPException(400, f"创建失败: {e}")

    # 重新查一遍拿 brand/series 名称（repo.flush 了但没 commit，关系数据可能没完全加载）
    return ok(_sku_to_detail(sku, db))


@router.put("/skus/{sku_id}", response_model=StandardResponse[SkuDetailResponse], summary="更新 SKU")
def update_sku(sku_id: int, body: SkuUpdateRequest, db: Session = Depends(get_db)):
    repo = SkuRepo(db)
    try:
        sku = repo.update(sku_id, **body.model_dump(exclude_none=True))
        db.commit()
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        db.rollback()
        # 唯一约束冲突 → 409，其他 DB 错误 → 500
        err_str = str(e)
        if "UniqueViolation" in err_str or "unique constraint" in err_str:
            raise HTTPException(409, detail={"message": "该商品已存在"})
        raise HTTPException(400, f"更新失败: {e}")
    return ok(_sku_to_detail(sku, db))


@router.delete("/skus/{sku_id}", response_model=StandardResponse[dict], summary="删除 SKU（级联清理子表，不动品牌/系列）")
def delete_sku(sku_id: int, db: Session = Depends(get_db)):
    repo = SkuRepo(db)
    ok_repo = repo.delete(sku_id)
    if not ok_repo:
        raise HTTPException(404, "SKU 不存在")
    db.commit()
    return ok({"deleted": True, "sku_id": sku_id})


# ============================================================================
# 内部：模型 → dict
# ============================================================================

def _brand_to_dict(b) -> dict[str, Any]:
    return {
        "id": b.id,
        "brand_no": b.brand_no,
        "name": b.name,
        "summary": b.summary,
        "brand_guide": b.brand_guide,
        "status": b.status,
        "series_count": b.series_count,
        "sku_count": b.sku_count,
        "created_at": b.created_at.isoformat(),
        "updated_at": b.updated_at.isoformat(),
    }


def _series_to_dict(s) -> dict[str, Any]:
    return {
        "id": s.id,
        "series_no": s.series_no,
        "name": s.name,
        "summary": s.summary,
        "brand_id": s.brand_id,
        "brand_name": s.brand.name if hasattr(s, "brand") and s.brand else "",
        "sku_count": s.sku_count,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


def _sku_to_list_item(sku, db: Session) -> SkuListItem:
    from wellflow.app.models.asset_models import ProductImage
    img_count = db.query(ProductImage).filter(ProductImage.sku_id == sku.id).count()
    return SkuListItem(
        id=sku.id,
        sku_no=sku.sku_no,
        name=sku.name,
        style_no=sku.style_no,
        brand_name=sku.brand.name if sku.brand else "",
        series_name=sku.series.name if sku.series else "",
        category=sku.category,
        color=sku.color,
        status=sku.status,
        image_count=img_count,
        created_at=sku.created_at.isoformat(),
        updated_at=sku.updated_at.isoformat(),
    )


def _sku_to_detail(sku, db: Session) -> SkuDetailResponse:
    images_out: list[SkuImageResponse] = []
    for img in sku.images or []:
        images_out.append(SkuImageResponse(
            id=img.id,
            category=img.category,
            storage_uri=img.storage_uri,
            url=_storage_uri_url(img.storage_uri),
            sort_order=img.sort_order,
            created_at=img.created_at.isoformat(),
        ))

    return SkuDetailResponse(
        id=sku.id,
        sku_no=sku.sku_no,
        name=sku.name,
        style_no=sku.style_no,
        category=sku.category,
        color=sku.color,
        material=sku.material,
        silhouette=sku.silhouette,
        season=sku.season,
        selling_points=sku.selling_points,
        brand_summary=sku.brand_summary,
        source_url=sku.source_url,
        status=sku.status,
        brand_id=sku.brand_id,
        brand_name=sku.brand.name if sku.brand else "",
        series_id=sku.series_id,
        series_name=sku.series.name if sku.series else "",
        images=images_out,
        image_count=len(images_out),
        created_at=sku.created_at.isoformat(),
        updated_at=sku.updated_at.isoformat(),
    )
