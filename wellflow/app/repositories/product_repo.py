"""产品域数据访问层（Brand / Series / SKU）。

关键约束：
  - brand / series 不在此处创建（find-or-create 逻辑在 API 层），只负责查询和更新
  - 删除时 brand/series 使用 RESTRICT，有子对象就拒绝
  - SKU 删除时级联清子表（image / historical_asset / knowledge_link），不动上层
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func, desc, update
from sqlalchemy.orm import Session

from wellflow.app.models.asset_models import (
    ProductBrand, ProductSeries, ProductSku,
    ProductImage, ProductHistoricalAsset, ProductKnowledgeLink,
)


# ============================================================================
# Brand Repo
# ============================================================================

class BrandRepo:
    def __init__(self, db: Session):
        self.db = db

    def get(self, brand_id: int) -> ProductBrand | None:
        return self.db.get(ProductBrand, brand_id)

    def get_or_create(self, brand_name: str) -> ProductBrand:
        """find-or-create 模式，供 SKU 创建时调用。"""
        obj = self.db.query(ProductBrand).filter(
            ProductBrand.name == brand_name.strip(),
            ProductBrand.status == "active",
        ).first()
        if obj is None:
            obj = ProductBrand(
                brand_no=_gen_no("BRAND", self.db, ProductBrand),
                name=brand_name.strip(),
            )
            self.db.add(obj)
            self.db.flush()  # 拿 id 但不 commit
        return obj

    def list(self) -> list[ProductBrand]:
        return list(
            self.db.execute(
                select(ProductBrand).where(ProductBrand.status == "active")
            ).scalars().all()
        )

    def update(self, brand_id: int, **kwargs: Any) -> ProductBrand:
        obj = self.get(brand_id)
        if obj is None:
            raise ValueError(f"brand {brand_id} 不存在")
        for k, v in kwargs.items():
            if v is not None and hasattr(obj, k):
                setattr(obj, k, v)
        self.db.flush()
        return obj

    def delete(self, brand_id: int) -> tuple[bool, str]:
        """删除品牌。有系列时返回 (False, error_msg)。"""
        brand = self.get(brand_id)
        if brand is None:
            return False, "品牌不存在"
        if brand.series_count > 0:
            return False, f"该品牌下还有 {brand.series_count} 个系列、{brand.sku_count} 个 SKU，无法删除"
        self.db.delete(brand)
        self.db.flush()
        return True, ""


# ============================================================================
# Series Repo
# ============================================================================

class SeriesRepo:
    def __init__(self, db: Session):
        self.db = db

    def get(self, series_id: int) -> ProductSeries | None:
        return self.db.get(ProductSeries, series_id)

    def get_or_create(self, brand_id: int, series_name: str) -> ProductSeries:
        """find-or-create 模式（必须绑定 brand）。"""
        obj = self.db.query(ProductSeries).filter(
            ProductSeries.brand_id == brand_id,
            ProductSeries.name == series_name.strip(),
        ).first()
        if obj is None:
            obj = ProductSeries(
                brand_id=brand_id,
                series_no=_gen_no("SERIES", self.db, ProductSeries),
                name=series_name.strip(),
            )
            self.db.add(obj)
            self.db.flush()
            # 更新 brand.series_count
            brand = self.db.get(ProductBrand, brand_id)
            if brand:
                brand.series_count += 1
        return obj

    def list_by_brand(self, brand_id: int) -> list[ProductSeries]:
        return list(
            self.db.execute(
                select(ProductSeries).where(ProductSeries.brand_id == brand_id)
            ).scalars().all()
        )

    def update(self, series_id: int, **kwargs: Any) -> ProductSeries:
        obj = self.get(series_id)
        if obj is None:
            raise ValueError(f"series {series_id} 不存在")

        old_brand_id = obj.brand_id
        new_brand_id = kwargs.pop("brand_id", None)

        for k, v in kwargs.items():
            if v is not None and hasattr(obj, k):
                setattr(obj, k, v)

        # brand 迁移：修正 usage_count
        if new_brand_id is not None and new_brand_id != old_brand_id:
            obj.brand_id = new_brand_id
            # 先 flush，让 brand_id 变更持久化，后续 COUNT 查询才能看到最新数据
            self.db.flush()

            old_brand = self.db.get(ProductBrand, old_brand_id)
            if old_brand:
                old_brand.sku_count -= obj.sku_count
                old_brand.series_count = max(0, old_brand.series_count - 1)
            new_brand = self.db.get(ProductBrand, new_brand_id)
            if new_brand:
                new_brand.sku_count += obj.sku_count
                # series_count 重新统计（flush 之后查才准确）
                new_brand.series_count = self.db.execute(
                    select(func.count(ProductSeries.id)).where(ProductSeries.brand_id == new_brand_id)
                ).scalar() or 0

        self.db.flush()
        return obj

    def delete(self, series_id: int) -> tuple[bool, str]:
        """删除系列。有 SKU 时返回 (False, error_msg)。"""
        series = self.get(series_id)
        if series is None:
            return False, "系列不存在"
        if series.sku_count > 0:
            return False, f"该系列下还有 {series.sku_count} 个 SKU，无法删除"

        # 先更新 brand.series_count
        brand = self.db.get(ProductBrand, series.brand_id)
        if brand:
            brand.series_count = max(0, brand.series_count - 1)
        self.db.delete(series)
        self.db.flush()
        return True, ""


# ============================================================================
# SKU Repo
# ============================================================================

class SkuRepo:
    def __init__(self, db: Session):
        self.db = db

    def get(self, sku_id: int) -> ProductSku | None:
        return self.db.get(ProductSku, sku_id)

    def create(
        self,
        *,
        series_id: int,
        brand_id: int,
        name: str,
        style_no: str,
        category: str | None = None,
        color: str | None = None,
        material: str | None = None,
        silhouette: str | None = None,
        season: str | None = None,
        selling_points: str | None = None,
        brand_summary: str | None = None,
        source_url: str | None = None,
        images: list[dict[str, Any]] | None = None,
        sku_no: str | None = None,
    ) -> ProductSku:
        """创建 SKU 主表记录。

        Args:
            sku_no: 可选。调用方已经提前算好 sku_no（如用于目录命名）时直接传入；
                    不传时内部会 :func:`_gen_no` 生成。
        """
        obj = ProductSku(
            sku_no=sku_no or _gen_no("SKU", self.db, ProductSku),
            series_id=series_id,
            brand_id=brand_id,
            name=name,
            style_no=style_no,
            category=category,
            color=color,
            material=material,
            silhouette=silhouette,
            season=season,
            selling_points=selling_points,
            brand_summary=brand_summary,
            source_url=source_url,
        )
        self.db.add(obj)
        self.db.flush()  # 拿 sku.id

        # 图片
        for img_data in images or []:
            self.db.add(ProductImage(
                sku_id=obj.id,
                category=img_data["category"],
                storage_uri=img_data["storage_uri"],
                sort_order=img_data.get("sort_order", 0),
            ))

        # 冗余计数
        series = self.db.get(ProductSeries, series_id)
        if series:
            series.sku_count += 1
        brand = self.db.get(ProductBrand, brand_id)
        if brand:
            brand.sku_count += 1
            # brand.series_count 重算一次（可能 find-or-create 时已加过，但以防万一）
            brand.series_count = self.db.execute(
                select(func.count(ProductSeries.id)).where(ProductSeries.brand_id == brand_id)
            ).scalar() or 0

        self.db.flush()
        return obj

    def find_duplicate(self, series_id: int, style_no: str, name: str) -> ProductSku | None:
        """按 (series_id, style_no, name) 三元组查重，精确匹配。"""
        return self.db.query(ProductSku).filter(
            ProductSku.series_id == series_id,
            ProductSku.style_no == style_no,
            ProductSku.name == name,
        ).first()

    def update(self, sku_id: int, **kwargs: Any) -> ProductSku:
        obj = self.get(sku_id)
        if obj is None:
            raise ValueError(f"sku {sku_id} 不存在")
        for k, v in kwargs.items():
            if v is not None and hasattr(obj, k):
                setattr(obj, k, v)
        obj.updated_at = datetime.now(timezone.utc)
        self.db.flush()
        return obj

    def delete(self, sku_id: int) -> bool:
        """删除 SKU 及其所有子表数据。"""
        sku = self.get(sku_id)
        if sku is None:
            return False

        # 子表（都是 CASCADE，但显式删一遍更清晰）
        self.db.query(ProductKnowledgeLink).where(ProductKnowledgeLink.sku_id == sku_id).delete(synchronize_session=False)
        self.db.query(ProductHistoricalAsset).where(ProductHistoricalAsset.sku_id == sku_id).delete(synchronize_session=False)
        self.db.query(ProductImage).where(ProductImage.sku_id == sku_id).delete(synchronize_session=False)

        # 主表
        self.db.delete(sku)

        # 修正品牌/系列的冗余计数
        series = self.db.get(ProductSeries, sku.series_id)
        if series:
            series.sku_count = max(0, series.sku_count - 1)
        brand = self.db.get(ProductBrand, sku.brand_id)
        if brand:
            brand.sku_count = max(0, brand.sku_count - 1)

        self.db.flush()
        return True

    def list(
        self,
        *,
        search: str | None = None,
        brand_id: int | None = None,
        series_id: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[ProductSku], int]:
        stmt = select(ProductSku)
        count_stmt = select(func.count(ProductSku.id))

        if search:
            like = f"%{search}%"
            stmt = stmt.where(ProductSku.name.ilike(like) | ProductSku.style_no.ilike(like) | ProductSku.sku_no.ilike(like))
            count_stmt = count_stmt.where(ProductSku.name.ilike(like) | ProductSku.style_no.ilike(like) | ProductSku.sku_no.ilike(like))
        if brand_id:
            stmt = stmt.where(ProductSku.brand_id == brand_id)
            count_stmt = count_stmt.where(ProductSku.brand_id == brand_id)
        if series_id:
            stmt = stmt.where(ProductSku.series_id == series_id)
            count_stmt = count_stmt.where(ProductSku.series_id == series_id)

        total = self.db.execute(count_stmt).scalar() or 0
        stmt = stmt.order_by(desc(ProductSku.created_at))
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        items = list(self.db.execute(stmt).scalars().all())
        return items, total


# ============================================================================
# 通用工具
# ============================================================================

def _gen_no(prefix: str, db: Session, model_cls) -> str:
    """自动生成唯一编号，如 BRAND-001 / SERIES-001 / SKU-000001。"""
    from sqlalchemy import select
    col = getattr(model_cls, f"{prefix.lower()}_no")
    rows = db.execute(select(col)).scalars().all()
    nums = []
    for val in rows:
        m = re.search(r"(\d+)$", val or "")
        if m:
            nums.append(int(m.group(1)))
    n = (max(nums) + 1) if nums else 1
    width = 6 if prefix == "SKU" else 3
    return f"{prefix}-{n:0{width}d}"
