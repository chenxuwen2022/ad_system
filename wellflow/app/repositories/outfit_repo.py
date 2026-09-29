# -*- coding: utf-8 -*-
"""穿搭库数据访问层(仿 mannequin_repo)。

Outfit 主表 + JSONB 嵌套(items/dims),dims 六组维度筛选用 PG JSONB
的 ?| 运算符(任意交集命中)。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import array as pg_array
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession

from wellflow.app.models.outfit_models import Outfit


class OutfitRepo:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get(self, outfit_id: int) -> Outfit | None:
        return self.db.get(Outfit, outfit_id)

    def get_by_no(self, outfit_no: str) -> Outfit | None:
        stmt = select(Outfit).where(Outfit.outfit_no == outfit_no)
        return self.db.execute(stmt).scalars().first()

    def create(
        self,
        *,
        name: str,
        desc: str | None = None,
        tags: str | None = None,
        scope: str = "mine",
        origin: str | None = "upload",
        cover_storage_uri: str | None = None,
        original_storage_uri: str | None = None,
        items: list[dict[str, Any]] | None = None,
        dims: dict[str, list[str]] | None = None,
    ) -> Outfit:
        obj = Outfit(
            outfit_no=_gen_outfit_no(self.db),
            name=name.strip(),
            desc=desc,
            tags=tags,
            scope=scope,
            origin=origin,
            cover_storage_uri=cover_storage_uri,
            original_storage_uri=original_storage_uri,
            items=items or [],
            dims=dims or {},
        )
        self.db.add(obj)
        self.db.flush()
        return obj

    def update(
        self,
        outfit_id: int,
        *,
        name: str | None = None,
        desc: str | None = None,
        tags: str | None = None,
        cover_storage_uri: str | None = None,
        original_storage_uri: str | None = None,
        items: list[dict[str, Any]] | None = None,
        dims: dict[str, list[str]] | None = None,
        status: str | None = None,
    ) -> Outfit:
        obj = self.get(outfit_id)
        if obj is None:
            raise ValueError(f"outfit {outfit_id} 不存在")
        if name is not None:
            obj.name = name.strip()
        if desc is not None:
            obj.desc = desc
        if tags is not None:
            obj.tags = tags
        if cover_storage_uri is not None:
            obj.cover_storage_uri = cover_storage_uri
        if original_storage_uri is not None:
            obj.original_storage_uri = original_storage_uri
        if items is not None:
            obj.items = items
        if dims is not None:
            obj.dims = dims
        if status is not None:
            obj.status = status
        obj.updated_at = datetime.now(timezone.utc)
        self.db.flush()
        return obj

    def delete(self, outfit_id: int) -> bool:
        obj = self.get(outfit_id)
        if obj is None:
            return False
        self.db.delete(obj)
        self.db.flush()
        return True

    # ------------------------------------------------------------------
    # 列表查询(scope / q / dims 筛选 + 分页)
    # ------------------------------------------------------------------

    def list(
        self,
        *,
        scope: str | None = None,
        q: str | None = None,
        dims: dict[str, list[str]] | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Outfit], int]:
        stmt = select(Outfit)
        count_stmt = select(func.count(Outfit.id))

        if scope and scope != "all":
            stmt = stmt.where(Outfit.scope == scope)
            count_stmt = count_stmt.where(Outfit.scope == scope)

        if q:
            like = f"%{q.strip()}%"
            cond = or_(
                Outfit.name.ilike(like),
                Outfit.outfit_no.ilike(like),
                Outfit.tags.ilike(like),
                Outfit.desc.ilike(like),
            )
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)

        if dims:
            # 组内多值 OR、组间 AND:JSONB 数组用 ?| 任意交集
            for dk, vals in dims.items():
                if not vals:
                    continue
                cond = Outfit.dims[dk].op("?|")(pg_array([str(v) for v in vals]))
                stmt = stmt.where(cond)
                count_stmt = count_stmt.where(cond)

        total = self.db.execute(count_stmt).scalar() or 0
        stmt = stmt.order_by(Outfit.created_at.desc())
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        items = list(self.db.execute(stmt).scalars().all())
        return items, total


    # ==================================================================
    # 异步变体(方案 B:async 端点的「立即入库」走 AsyncSession;
    # 后台 daemon 线程继续用上面的同步方法,互不影响)
    # ==================================================================

    async def aget(self, outfit_id: int) -> Outfit | None:
        return await self.db.get(Outfit, outfit_id)

    async def acreate(
        self,
        *,
        name: str,
        desc: str | None = None,
        tags: str | None = None,
        scope: str = "mine",
        origin: str | None = "upload",
        cover_storage_uri: str | None = None,
        original_storage_uri: str | None = None,
        items: list[dict[str, Any]] | None = None,
        dims: dict[str, list[str]] | None = None,
    ) -> Outfit:
        obj = Outfit(
            outfit_no=await _agen_outfit_no(self.db),
            name=name.strip(),
            desc=desc,
            tags=tags,
            scope=scope,
            origin=origin,
            cover_storage_uri=cover_storage_uri,
            original_storage_uri=original_storage_uri,
            items=items or [],
            dims=dims or {},
        )
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def aupdate(
        self,
        outfit_id: int,
        *,
        name: str | None = None,
        desc: str | None = None,
        tags: str | None = None,
        cover_storage_uri: str | None = None,
        original_storage_uri: str | None = None,
        items: list[dict[str, Any]] | None = None,
        dims: dict[str, list[str]] | None = None,
        status: str | None = None,
    ) -> Outfit:
        obj = await self.aget(outfit_id)
        if obj is None:
            raise ValueError(f"outfit {outfit_id} 不存在")
        if name is not None:
            obj.name = name.strip()
        if desc is not None:
            obj.desc = desc
        if tags is not None:
            obj.tags = tags
        if cover_storage_uri is not None:
            obj.cover_storage_uri = cover_storage_uri
        if original_storage_uri is not None:
            obj.original_storage_uri = original_storage_uri
        if items is not None:
            obj.items = items
        if dims is not None:
            obj.dims = dims
        if status is not None:
            obj.status = status
        obj.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        return obj

    async def adelete(self, outfit_id: int) -> bool:
        obj = await self.aget(outfit_id)
        if obj is None:
            return False
        await self.db.delete(obj)
        await self.db.flush()
        return True

    async def alist(
        self,
        *,
        scope: str | None = None,
        q: str | None = None,
        dims: dict[str, list[str]] | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Outfit], int]:
        stmt = select(Outfit)
        count_stmt = select(func.count(Outfit.id))

        if scope and scope != "all":
            stmt = stmt.where(Outfit.scope == scope)
            count_stmt = count_stmt.where(Outfit.scope == scope)

        if q:
            like = f"%{q.strip()}%"
            cond = or_(
                Outfit.name.ilike(like),
                Outfit.outfit_no.ilike(like),
                Outfit.tags.ilike(like),
                Outfit.desc.ilike(like),
            )
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)

        if dims:
            for dk, vals in dims.items():
                if not vals:
                    continue
                cond = Outfit.dims[dk].op("?|")(pg_array([str(v) for v in vals]))
                stmt = stmt.where(cond)
                count_stmt = count_stmt.where(cond)

        total = (await self.db.execute(count_stmt)).scalar() or 0
        stmt = stmt.order_by(Outfit.created_at.desc())
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        items = list((await self.db.execute(stmt)).scalars().all())
        return items, total


# ============================================================================
# 通用工具
# ============================================================================

def _gen_outfit_no(db: Session) -> str:
    from uuid import uuid4
    return "WF-O" + uuid4().hex[:16].upper()


async def _agen_outfit_no(db: AsyncSession) -> str:
    from uuid import uuid4
    return "WF-O" + uuid4().hex[:16].upper()
