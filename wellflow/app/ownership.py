"""Wellflow ownership policy, shared by sync/async sessions and background jobs.

Request scope is immutable and inherited by asyncio tasks / asyncio.to_thread.
No scope is reserved for internal maintenance and offline migrations, never HTTP.
"""
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import event, inspect, select, true
from sqlalchemy.orm import Session, with_loader_criteria


@dataclass(frozen=True)
class Principal:
    user_id: int
    company_id: int
    role: str


principal: ContextVar[Principal | None] = ContextVar("wellflow_principal", default=None)


def models():
    from wellflow.app.models.task_models import Conversation, Task, ChatMessage, TaskEvent, TaskImage, TaskErrorLog
    from wellflow.app.models.asset_models import ProductBrand, ProductSeries, ProductSku, ProductImage, ProductHistoricalAsset, ProductKnowledgeLink
    from wellflow.app.models.mannequin_models import Mannequin, MannequinTag, MannequinGenerateLog
    from wellflow.app.models.scene_models import Scene
    from wellflow.app.models.outfit_models import Outfit
    roots = (Conversation, Task, ProductBrand, ProductSeries, ProductSku, Mannequin, Scene, Outfit, MannequinGenerateLog)
    children = {
        ChatMessage: (Conversation, "conversation_id", "conversation_id"),
        TaskEvent: (Task, "task_id", "task_id"),
        TaskImage: (Task, "task_id", "task_id"),
        TaskErrorLog: (Task, "task_id", "task_id"),
        ProductImage: (ProductSku, "sku_id", "id"),
        ProductHistoricalAsset: (ProductSku, "sku_id", "id"),
        ProductKnowledgeLink: (ProductSku, "sku_id", "id"),
        MannequinTag: (Mannequin, "mannequin_id", "id"),
    }
    return roots, children


def criterion(cls, actor, *, write=False):
    if actor.role == "platform_admin":
        return true()
    rule = cls.company_id == actor.company_id
    if actor.role == "company_user" and (write or cls.__tablename__ in {"conversation", "task", "mannequin_generate_log"}):
        rule = rule & (cls.owner_id == actor.user_id)
    return rule


@event.listens_for(Session, "do_orm_execute")
def restrict_queries(state):
    actor = principal.get()
    if actor is None or actor.role == "platform_admin":
        return
    roots, children = models()
    statement = state.statement
    if not state.is_orm_statement:
        return
    write = state.is_update or state.is_delete
    for cls in roots:
        statement = statement.options(with_loader_criteria(cls, criterion(cls, actor, write=write), include_aliases=True))
    for cls, (parent, fk, pk) in children.items():
        allowed = select(getattr(parent, pk)).where(criterion(parent, actor, write=write))
        statement = statement.options(with_loader_criteria(cls, getattr(cls, fk).in_(allowed), include_aliases=True))
    state.statement = statement


def ensure_write(obj, actor=None):
    actor = actor or principal.get()
    if actor is None or actor.role == "platform_admin":
        return
    if obj.company_id != actor.company_id or (actor.role == "company_user" and obj.owner_id != actor.user_id):
        raise HTTPException(403, "只能修改或删除自己创建的资产")


@event.listens_for(Session, "before_flush")
def protect_writes(db, _context, _instances):
    actor = principal.get()
    if actor is None:
        return
    roots, children = models()
    from wellflow.app.models.task_models import Task, Conversation
    from wellflow.app.models.asset_models import ProductBrand, ProductSeries, ProductSku
    from wellflow.app.models.mannequin_models import Mannequin, MannequinGenerateLog
    references = {
        ProductSeries: (("brand_id", ProductBrand),),
        ProductSku: (("brand_id", ProductBrand), ("series_id", ProductSeries)),
        Conversation: (("sku_id", ProductSku),),
        Task: (("conversation_id", Conversation),),
        MannequinGenerateLog: (("mannequin_id", Mannequin), ("parent_log_id", MannequinGenerateLog)),
    }
    for obj in list(db.new) + list(db.dirty) + list(db.deleted):
        if isinstance(obj, roots):
            new = obj in db.new
            if new:
                obj.company_id = actor.company_id
                obj.owner_id = actor.user_id
            else:
                changed = {a.key for a in inspect(obj).attrs if a.history.has_changes()}
                if changed & {"company_id", "owner_id"}:
                    raise HTTPException(403, "不允许变更数据归属")
                # Aggregate counters are maintained while creating shared SKU children.
                counters_only = isinstance(obj, (ProductBrand, ProductSeries)) and changed <= {"sku_count", "series_count", "updated_at"} and obj not in db.deleted
                if not counters_only:
                    ensure_write(obj, actor)
            for reference_index, (field, parent_cls) in enumerate(references.get(type(obj), ())):
                value = getattr(obj, field)
                if value is None:
                    continue
                parent = db.get(parent_cls, value)
                if parent is None:
                    raise HTTPException(404, "关联资源不存在或无权访问")
                if new and actor.role == "platform_admin" and reference_index == 0:
                    obj.company_id = parent.company_id
                if new and isinstance(obj, Task):
                    obj.company_id, obj.owner_id = parent.company_id, parent.owner_id
                if parent.company_id != obj.company_id:
                    raise HTTPException(403, "关联资源必须属于同一公司")
        elif type(obj) in children:
            parent_cls, fk, _ = children[type(obj)]
            parent = db.get(parent_cls, getattr(obj, fk))
            if parent is None:
                raise HTTPException(404, "关联资源不存在或无权访问")
            # Generated history can be attached to any accessible SKU by its task;
            # editing original images/tags still requires ownership.
            generated_image = type(obj).__name__ == "ProductImage" and obj in db.new and obj.image_type == "ad" and obj.source_task_id
            if generated_image:
                source = db.get(Task, obj.source_task_id)
                if source is None or source.company_id != parent.company_id:
                    raise HTTPException(403, "无权将此任务图片入库")
            append_history = obj in db.new and type(obj).__name__ in {"ProductHistoricalAsset", "ProductKnowledgeLink"}
            if not generated_image and not append_history:
                ensure_write(parent, actor)
