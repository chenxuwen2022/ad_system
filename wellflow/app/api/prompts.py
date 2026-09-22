"""Manage prompt revisions and atomic category releases."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wellflow.app.database import get_db
from wellflow.app.models.prompt_models import PromptRelease, PromptReleaseItem, PromptRevision, PromptTemplate
from wellflow.app.prompt.registry import CATEGORIES, seed_prompts

router = APIRouter(prefix="/prompts", tags=["提示词管理"])


class RevisionInput(BaseModel):
    content: str = Field(min_length=1)
    note: str = Field(default="", max_length=255)


class ReleaseInput(BaseModel):
    note: str = Field(default="", max_length=255)


class PromptCreate(BaseModel):
    key: str = Field(min_length=2, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    category: str
    name: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1)
    note: str = Field(default="", max_length=255)


class PromptUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1)
    note: str = Field(default="", max_length=255)
    expected_revision_id: int


def _template(db: Session, key: str, *, include_archived: bool = False):
    item = db.get(PromptTemplate, key)
    if not item or (item.is_archived and not include_archived):
        raise HTTPException(404, "提示词不存在")
    return item


def _release(db: Session, category: str):
    return db.scalar(select(PromptRelease).where(PromptRelease.category == category).order_by(PromptRelease.number.desc()).limit(1))


def _prompt_detail(db: Session, template: PromptTemplate):
    draft = db.get(PromptRevision, template.draft_revision_id)
    published = _release(db, template.category)
    snapshot = db.get(PromptReleaseItem, (published.id, template.key)) if published else None
    active = db.get(PromptRevision, snapshot.revision_id) if snapshot else None
    return {"key": template.key, "category": template.category, "name": template.name,
        "draft_revision_id": draft.id, "draft_number": draft.number, "draft_content": draft.content,
        "published_revision_id": active.id if active else None,
        "published_number": active.number if active else None,
        "changed": not active or draft.id != active.id}


@router.get("")
def list_prompts(db: Session = Depends(get_db)):
    seed_prompts(db)
    result = []
    for category, label in CATEGORIES.items():
        published = _release(db, category)
        templates = db.scalars(select(PromptTemplate).where(PromptTemplate.category == category, PromptTemplate.is_archived.is_(False)).order_by(PromptTemplate.key)).all()
        items = [_prompt_detail(db, template) for template in templates]
        published_keys = set(db.scalars(select(PromptReleaseItem.template_key).where(PromptReleaseItem.release_id == published.id))) if published else set()
        changed = published_keys != {template.key for template in templates} or any(item["changed"] for item in items)
        result.append({"key": category, "name": label, "version": published.number if published else 0, "changed": changed, "items": items})
    return result


@router.post("", status_code=201)
def create_prompt(body: PromptCreate, db: Session = Depends(get_db)):
    seed_prompts(db)
    if body.category not in CATEGORIES:
        raise HTTPException(422, "分类不存在")
    existing = db.get(PromptTemplate, body.key)
    if existing and not existing.is_archived:
        raise HTTPException(409, "提示词标识已存在")
    if existing and existing.category != body.category:
        raise HTTPException(409, "历史提示词不能更换分类")
    template = existing or PromptTemplate(key=body.key, category=body.category, name=body.name)
    template.name = body.name
    template.is_archived = False
    db.add(template)
    db.flush()
    number = (db.scalar(select(func.max(PromptRevision.number)).where(PromptRevision.template_key == body.key)) or 0) + 1
    revision = PromptRevision(template_key=body.key, number=number, content=body.content, note=body.note)
    db.add(revision)
    db.flush()
    template.draft_revision_id = revision.id
    db.commit()
    return _prompt_detail(db, template)


@router.get("/{key}")
def get_prompt(key: str, db: Session = Depends(get_db)):
    seed_prompts(db)
    return _prompt_detail(db, _template(db, key))


@router.put("/{key}")
def update_prompt(key: str, body: PromptUpdate, db: Session = Depends(get_db)):
    seed_prompts(db)
    template = _template(db, key)
    if template.draft_revision_id != body.expected_revision_id:
        raise HTTPException(409, "提示词已被其他修改更新，请刷新后重试")
    current = db.get(PromptRevision, template.draft_revision_id)
    if current.content == body.content and template.name == body.name:
        raise HTTPException(409, "没有修改内容")
    number = current.number + 1
    revision = PromptRevision(template_key=key, number=number, content=body.content, note=body.note)
    db.add(revision)
    db.flush()
    template.name = body.name
    template.draft_revision_id = revision.id
    db.commit()
    return _prompt_detail(db, template)


@router.delete("/{key}")
def delete_prompt(key: str, db: Session = Depends(get_db)):
    seed_prompts(db)
    template = _template(db, key)
    template.is_archived = True
    db.commit()
    return {"key": key, "archived": True, "publish_required": True}


@router.get("/{key}/revisions")
def revisions(key: str, db: Session = Depends(get_db)):
    seed_prompts(db)
    _template(db, key)
    rows = db.scalars(select(PromptRevision).where(PromptRevision.template_key == key).order_by(PromptRevision.number.desc())).all()
    return [{"id": row.id, "number": row.number, "content": row.content, "note": row.note, "created_at": row.created_at.isoformat()} for row in rows]


@router.post("/{key}/revisions")
def save_revision(key: str, body: RevisionInput, db: Session = Depends(get_db)):
    seed_prompts(db)
    template = _template(db, key)
    number = (db.scalar(select(func.max(PromptRevision.number)).where(PromptRevision.template_key == key)) or 0) + 1
    revision = PromptRevision(template_key=key, number=number, content=body.content, note=body.note)
    db.add(revision)
    db.flush()
    template.draft_revision_id = revision.id
    db.commit()
    return {"id": revision.id, "number": number}


@router.post("/categories/{category}/release")
def publish(category: str, body: ReleaseInput, db: Session = Depends(get_db)):
    seed_prompts(db)
    if category not in CATEGORIES:
        raise HTTPException(404, "分类不存在")
    current = _release(db, category)
    templates = db.scalars(select(PromptTemplate).where(PromptTemplate.category == category)).all()
    templates = [template for template in templates if not template.is_archived]
    previous_items = {item.template_key: item.revision_id for item in db.scalars(select(PromptReleaseItem).where(PromptReleaseItem.release_id == current.id))} if current else {}
    changed = previous_items != {template.key: template.draft_revision_id for template in templates}
    if not changed:
        raise HTTPException(409, "没有待发布的修改")
    release = PromptRelease(category=category, number=(current.number if current else 0) + 1, note=body.note)
    db.add(release)
    db.flush()
    for template in templates:
        db.add(PromptReleaseItem(release_id=release.id, template_key=template.key, revision_id=template.draft_revision_id))
    db.commit()
    return {"id": release.id, "version": release.number}


@router.get("/categories/{category}/releases")
def releases(category: str, db: Session = Depends(get_db)):
    seed_prompts(db)
    if category not in CATEGORIES:
        raise HTTPException(404, "分类不存在")
    rows = db.scalars(select(PromptRelease).where(PromptRelease.category == category).order_by(PromptRelease.number.desc())).all()
    result = []
    for row in rows:
        items = db.execute(select(PromptReleaseItem.template_key, PromptRevision.number).join(PromptRevision, PromptReleaseItem.revision_id == PromptRevision.id).where(PromptReleaseItem.release_id == row.id)).all()
        result.append({"id": row.id, "version": row.number, "note": row.note, "created_at": row.created_at.isoformat(), "items": [{"key": key, "revision": number} for key, number in items]})
    return result
