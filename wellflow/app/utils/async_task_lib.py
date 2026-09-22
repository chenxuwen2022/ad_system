# -*- coding: utf-8 -*-
"""三库(穿搭/场景/模特)共享的异步任务设施(企业级:任务状态入 DB,多 worker 友好)。

统一封装:任务状态表读写(save/load/sweep,心跳判死)/ 后台协程注册防 GC /
生成图下载(SSL 降级重试 + 图片魔数校验)。
穿搭库、场景库、模特库共用本模块 —— 统一优化只改这一个文件。

判死机制(支持多 worker):status=processing 且 heartbeat_at 超 stale_seconds
未刷新 → 判 failed;不再依赖进程 pid(多进程部署互不误杀)。
"""

from __future__ import annotations

import asyncio
import base64
import json as json_mod
import time
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import (
    BIGINT, Column, DateTime, MetaData, String, Table, Text, delete, or_, select, update,
)
from sqlalchemy.dialects.postgresql import JSONB

_ASYNC_TASK = Table(
    "async_task",
    MetaData(),
    Column("id", BIGINT, primary_key=True),
    Column("task_id", String(64), unique=True, nullable=False),
    Column("task_type", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("step", String(32), nullable=True),
    Column("error", Text, nullable=True),
    Column("payload", JSONB, nullable=True),
    Column("row_kind", String(16), nullable=True),
    Column("row_id", BIGINT, nullable=True),
    Column("heartbeat_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class TaskStore:
    """任务状态存储(DB 版):save/load/sweep/heartbeat。

    构造签名与旧文件版保持一致(task_dir 参数保留但不再使用,历史目录兼容),
    三个库的调用方式零改动。
    """

    def __init__(
        self,
        task_dir=None,
        *,
        pid: str | None = None,
        ttl_seconds: float = 24 * 3600,
        stale_seconds: float = 90.0,
        row_id_field: str = "outfit_id",
        mark_row_failed: Callable[[int], None] | None = None,
    ):
        self.task_dir = task_dir  # 兼容保留(DB 版不使用)
        self.pid = pid
        self.ttl_seconds = ttl_seconds
        self.stale_seconds = stale_seconds
        self.row_id_field = row_id_field
        self.mark_row_failed = mark_row_failed

    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def save(self, task: dict) -> None:
        """保存任务状态(upsert,payload 存完整任务数据)。"""
        from wellflow.app.database import session_scope

        now = self._now()
        row_id = task.get(self.row_id_field)
        with session_scope() as db:
            existing = db.execute(
                select(_ASYNC_TASK.c.id).where(_ASYNC_TASK.c.task_id == task["task_id"])
            ).scalar()
            values = {
                "task_type": task.get("task_type", ""),
                "status": task.get("status", "processing"),
                "step": task.get("step"),
                "error": task.get("error"),
                "payload": task,
                "row_kind": self.row_id_field,
                "row_id": int(row_id) if row_id is not None else None,
                "heartbeat_at": now,
                "updated_at": now,
            }
            if existing is None:
                db.execute(_ASYNC_TASK.insert().values(
                    task_id=task["task_id"], created_at=now, **values))
            else:
                db.execute(update(_ASYNC_TASK)
                           .where(_ASYNC_TASK.c.task_id == task["task_id"])
                           .values(**values))
            db.commit()

    def heartbeat(self, task_id: str) -> None:
        """刷新心跳(不更新 payload)。"""
        from wellflow.app.database import session_scope
        with session_scope() as db:
            db.execute(update(_ASYNC_TASK)
                       .where(_ASYNC_TASK.c.task_id == task_id)
                       .values(heartbeat_at=self._now(), updated_at=self._now()))
            db.commit()

    def load(self, task_id: str) -> dict | None:
        """读任务(payload 即完整任务 dict)。"""
        from wellflow.app.database import session_scope
        with session_scope() as db:
            row = db.execute(
                select(_ASYNC_TASK).where(_ASYNC_TASK.c.task_id == task_id)
            ).first()
            if row is None:
                return None
            payload = row.payload
            if isinstance(payload, dict):
                # 列值为准(status 可能被 sweep 修改过)
                payload["status"] = row.status
                payload["error"] = row.error or payload.get("error")
                return payload
            return {
                "task_id": row.task_id,
                "task_type": row.task_type,
                "status": row.status,
                "step": row.step,
                "error": row.error,
            }

    def sweep(self) -> None:
        """自愈(多 worker 安全,心跳判死):
        1. status=processing 且 heartbeat_at 超 stale_seconds → 判 failed(联动业务行)
        2. updated_at 超 ttl_seconds → 删除
        """
        from wellflow.app.database import session_scope

        now = self._now()
        with session_scope() as db:
            # 判死
            cutoff = datetime.fromtimestamp(now.timestamp() - self.stale_seconds, tz=timezone.utc)
            stale_rows = db.execute(
                select(_ASYNC_TASK)
                .where(
                    _ASYNC_TASK.c.status == "processing",
                    or_(
                        _ASYNC_TASK.c.heartbeat_at.is_(None),
                        _ASYNC_TASK.c.heartbeat_at < cutoff,
                    ),
                )
            ).all()
            for row in stale_rows:
                if row.row_id and self.mark_row_failed:
                    self.mark_row_failed(int(row.row_id))
                db.execute(update(_ASYNC_TASK)
                           .where(_ASYNC_TASK.c.id == row.id)
                           .values(status="failed",
                                   error="服务重启或任务超时中断,请重试",
                                   updated_at=now))
            # 过期清理
            db.execute(delete(_ASYNC_TASK).where(
                _ASYNC_TASK.c.updated_at
                < datetime.fromtimestamp(now.timestamp() - self.ttl_seconds, tz=timezone.utc)))
            db.commit()


class BackgroundTasks:
    """后台协程注册表:create_task 并持有引用,防任务被 GC。"""

    def __init__(self):
        self._tasks: set = set()

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)


def spawn_heartbeat(store: TaskStore, task_id: str, interval: float = 20.0) -> None:
    """为后台任务挂周期心跳(自退出:任务结束自动停止)。

    每 interval 秒刷新 heartbeat_at;检测到任务已非 processing 自动退出,
    调用方无需手动 cancel。
    """
    async def _loop():
        try:
            while True:
                current = await asyncio.to_thread(store.load, task_id)
                if not current or current.get("status") != "processing":
                    return
                await asyncio.to_thread(store.heartbeat, task_id)
                await asyncio.sleep(interval)
        except Exception:
            pass

    asyncio.create_task(_loop())


def download_image(url: str, model: str) -> str:
    """下载网关返回的生成图 url → b64(SSL 降级重试 + 图片魔数校验)。"""
    import ssl
    import urllib.request as _ur

    data = None
    errors = []
    contexts = [None, ssl._create_unverified_context()]
    for ctx in contexts:
        try:
            with _ur.urlopen(url, timeout=60, context=ctx) as resp:
                data = resp.read()
            break
        except Exception as e:
            errors.append(str(e)[:90])
    if data is None:
        raise RuntimeError(f"{model}: 下载生成图失败: {errors[-1]}")
    if len(data) < 64 or data[:8] not in (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"):
        raise RuntimeError(f"{model}: 下载内容不是图片({data[:16]!r})")
    return base64.b64encode(data).decode()
