"""任务数据访问层。业务节点只调用 repo 方法，不直接写 SQL/ORM。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func, desc
from sqlalchemy.orm import Session

from wellflow.app.models.task_models import (
    Task, TaskEvent, TaskErrorLog, TaskImage,
)


class TaskRepo:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> Task | None:
        return self.db.get(Task, task_id)

    def create(
        self,
        task_id: str,
        request_json: dict[str, Any],
        phase: str = "input",
        brand_config_json: dict[str, Any] | None = None,
        conversation_id: str | None = None,
    ) -> Task:
        obj = Task(
            task_id=task_id,
            conversation_id=conversation_id,
            phase=phase,
            request_json=request_json,
            brand_config_json=brand_config_json or {},
        )
        self.db.add(obj)
        self.db.commit()
        return obj

    def update_phase(self, task_id: str, phase: str) -> None:
        obj = self.get(task_id)
        if obj:
            obj.phase = phase
            obj.updated_at = datetime.now(timezone.utc)
            self.db.commit()

    def save_interrupt(self, task_id: str, interrupt_json: dict[str, Any] | None) -> None:
        obj = self.get(task_id)
        if obj:
            obj.interrupt_json = interrupt_json
            obj.updated_at = datetime.now(timezone.utc)
            self.db.commit()

    def save_selected_plans(self, task_id: str, plan_ids: list[str]) -> None:
        obj = self.get(task_id)
        if obj:
            obj.selected_plan_ids_json = plan_ids
            obj.updated_at = datetime.now(timezone.utc)
            self.db.commit()

    # ------------------------------------------------------------------
    # 任务关联图片（模特图 + 生图成品）
    # ------------------------------------------------------------------

    def save_images(self, task_id: str, images: list[dict[str, Any]]) -> list[str]:
        """批量写入 task_image 行，返回 image_id 列表。

        images 每项字段：
          - image_type: "model" | "output"（必填）
          - storage_uri: 相对路径或远程 url（必填）
          - shot_id / prompt / prompt_index / variant_index（output 可选）
        """
        ids: list[str] = []
        for img in images:
            image_id = str(img.get("image_id") or uuid.uuid4().hex[:12])
            obj = TaskImage(
                image_id=image_id,
                task_id=task_id,
                image_type=img["image_type"],
                storage_uri=img["storage_uri"],
                shot_id=img.get("shot_id"),
                prompt=img.get("prompt"),
                prompt_index=img.get("prompt_index"),
                variant_index=img.get("variant_index"),
            )
            self.db.add(obj)
            ids.append(image_id)
        self.db.commit()
        return ids

    def list_images(self, task_id: str) -> list[TaskImage]:
        """按 task_id 查全部关联图片，按创建顺序返回。"""
        stmt = (
            select(TaskImage)
            .where(TaskImage.task_id == task_id)
            .order_by(TaskImage.created_at, TaskImage.image_id)
        )
        return list(self.db.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # 列表查询
    # ------------------------------------------------------------------

    def list_tasks(
        self,
        page: int = 1,
        page_size: int = 20,
        phase: str | None = None,
    ) -> tuple[list[Task], int]:
        """返回 (items, total_count)。"""
        stmt = select(Task)
        count_stmt = select(func.count(Task.task_id))
        if phase:
            stmt = stmt.where(Task.phase == phase)
            count_stmt = count_stmt.where(Task.phase == phase)

        total = self.db.execute(count_stmt).scalar() or 0
        stmt = stmt.order_by(desc(Task.created_at))
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        items = list(self.db.execute(stmt).scalars().all())
        return items, total

    # ------------------------------------------------------------------
    # 事件 / 错误
    # ------------------------------------------------------------------

    def add_event(
        self,
        task_id: str,
        event_type: str,
        phase: str | None = None,
        payload_json: dict[str, Any] | None = None,
        cost_usd: float | None = None,
    ) -> int:
        ev = TaskEvent(
            task_id=task_id,
            event_type=event_type,
            phase=phase,
            payload_json=payload_json or {},
            cost_usd=cost_usd,
        )
        self.db.add(ev)
        self.db.commit()
        return ev.event_id

    def add_error(
        self,
        task_id: str,
        code: str,
        category: str,
        message: str,
        source: str = "",
        retryable: bool = False,
        details_json: dict[str, Any] | None = None,
    ) -> int:
        err = TaskErrorLog(
            task_id=task_id,
            code=code,
            category=category,
            source=source,
            message=message,
            retryable=retryable,
            details_json=details_json or {},
        )
        self.db.add(err)
        self.db.commit()
        return err.error_id

    # ------------------------------------------------------------------
    # 删除任务（级联清理所有子表 + 主表）
    # ------------------------------------------------------------------

    def delete_task(self, task_id: str) -> bool:
        """删除任务及所有关联数据，按 FK 依赖顺序清理子表 + 主表。

        当前子表（均直接挂 Task，无跨子表依赖）：
          TaskImage / TaskEvent / TaskErrorLog → Task

        Returns:
            True = 主表 Task 行存在且已删除；False = task_id 不存在
        """
        task = self.db.get(Task, task_id)
        if not task:
            return False

        # 删直接挂 Task 的子表
        self.db.query(TaskImage).where(TaskImage.task_id == task_id).delete(synchronize_session=False)
        self.db.query(TaskEvent).where(TaskEvent.task_id == task_id).delete(synchronize_session=False)
        self.db.query(TaskErrorLog).where(TaskErrorLog.task_id == task_id).delete(synchronize_session=False)

        # 最后删主表
        self.db.delete(task)
        self.db.commit()
        return True
