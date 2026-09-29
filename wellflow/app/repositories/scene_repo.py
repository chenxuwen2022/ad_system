# -*- coding: utf-8 -*-
"""场景库数据访问层(仿 outfit_repo 的 asyncio 形态:全 async 方法)。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import array as pg_array
from sqlalchemy.ext.asyncio import AsyncSession

from wellflow.app.models.scene_models import Scene


class SceneRepo:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # CRUD(全 async)
    # ------------------------------------------------------------------

    async def aget(self, scene_id: int) -> Scene | None:
        return await self.db.get(Scene, scene_id)

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
        dims: dict[str, list[str]] | None = None,
    ) -> Scene:
        obj = Scene(
            scene_no=await _agen_scene_no(self.db),
            name=name.strip(),
            desc=desc,
            tags=tags,
            scope=scope,
            origin=origin,
            cover_storage_uri=cover_storage_uri,
            original_storage_uri=original_storage_uri,
            dims=dims or {},
        )
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def aupdate(
        self,
        scene_id: int,
        *,
        name: str | None = None,
        desc: str | None = None,
        tags: str | None = None,
        cover_storage_uri: str | None = None,
        original_storage_uri: str | None = None,
        dims: dict[str, list[str]] | None = None,
        status: str | None = None,
    ) -> Scene:
        obj = await self.aget(scene_id)
        if obj is None:
            raise ValueError(f"scene {scene_id} 不存在")
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
        if dims is not None:
            obj.dims = dims
        if status is not None:
            obj.status = status
        obj.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        return obj

    async def adelete(self, scene_id: int) -> bool:
        obj = await self.aget(scene_id)
        if obj is None:
            return False
        await self.db.delete(obj)
        await self.db.flush()
        return True

    # ------------------------------------------------------------------
    # 列表查询(scope / q / dims 筛选 + 分页)
    # ------------------------------------------------------------------

    async def alist(
        self,
        *,
        scope: str | None = None,
        q: str | None = None,
        dims: dict[str, list[str]] | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Scene], int]:
        stmt = select(Scene)
        count_stmt = select(func.count(Scene.id))

        if scope and scope != "all":
            stmt = stmt.where(Scene.scope == scope)
            count_stmt = count_stmt.where(Scene.scope == scope)

        if q:
            like = f"%{q.strip()}%"
            cond = or_(
                Scene.name.ilike(like),
                Scene.scene_no.ilike(like),
                Scene.tags.ilike(like),
                Scene.desc.ilike(like),
            )
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)

        if dims:
            # 组内多值 OR、组间 AND:JSONB 数组用 ?| 任意交集(与穿搭库同款)
            for dk, vals in dims.items():
                if not vals:
                    continue
                cond = Scene.dims[dk].op("?|")(pg_array([str(v) for v in vals]))
                stmt = stmt.where(cond)
                count_stmt = count_stmt.where(cond)

        total = (await self.db.execute(count_stmt)).scalar() or 0
        stmt = stmt.order_by(Scene.created_at.desc())
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        items = list((await self.db.execute(stmt)).scalars().all())
        return items, total


async def _agen_scene_no(db: AsyncSession) -> str:
    from uuid import uuid4
    return "WF-S" + uuid4().hex[:16].upper()
