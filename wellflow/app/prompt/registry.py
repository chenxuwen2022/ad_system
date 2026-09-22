"""Prompt catalog, seed data, and active release resolution."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wellflow.app.models.prompt_models import PromptRelease, PromptReleaseItem, PromptRevision, PromptTemplate
from wellflow.app.prompt import constant

CATEGORIES = {
    "commerce": "商拍提示词",
    "refine": "微调提示词",
    "intent": "意图识别提示词",
}
CATALOG = (
    ("product_report", "commerce", "生产品报告", "PRODUCT_ANALYZER_SYSTEM_PROMPT"),
    ("shoot_plan", "commerce", "生商拍策划", "PLANNING_AGENT_SYSTEM_PROMPT"),
    ("image_prompt", "commerce", "生图提示词", "GENERATE_PROMPT_FOR_IMAGE"),
    ("refine_report", "refine", "产品报告微调", "REFINE_NODE1_REPORT_SYSTEM_PROMPT"),
    ("refine_plan", "refine", "商拍策划微调", "REFINE_NODE2_SCHEMES_SYSTEM_PROMPT"),
    ("refine_image_prompt", "refine", "生图提示词微调", "REFINE_NODE3_PROMPTS_SYSTEM_PROMPT"),
    ("intent_classifier", "intent", "意图识别", "CLASSIFIER_SYSTEM"),
)
DEFAULTS = {key: getattr(constant, symbol) for key, _, _, symbol in CATALOG}


def seed_prompts(db: Session):
    """Initialize each category as a complete v1 snapshot, once per database."""
    if db.scalar(select(func.count()).select_from(PromptTemplate)):
        return
    for category in CATEGORIES:
        release = PromptRelease(category=category, number=1, note="从代码初始化")
        db.add(release)
        db.flush()
        for key, item_category, name, _ in CATALOG:
            if item_category != category:
                continue
            template = PromptTemplate(key=key, category=category, name=name)
            db.add(template)
            db.flush()
            revision = PromptRevision(template_key=key, number=1, content=DEFAULTS[key], note="初始版本")
            db.add(revision)
            db.flush()
            template.draft_revision_id = revision.id
            db.add(PromptReleaseItem(release_id=release.id, template_key=key, revision_id=revision.id))
    db.commit()


def active_prompt(db: Session, key: str) -> str:
    """Read the published snapshot; callers may use DEFAULTS during a database outage."""
    template = db.get(PromptTemplate, key)
    if not template:
        return DEFAULTS[key]
    release = db.scalar(select(PromptRelease).where(PromptRelease.category == template.category).order_by(PromptRelease.number.desc()).limit(1))
    if not release:
        return DEFAULTS[key]
    item = db.get(PromptReleaseItem, (release.id, key))
    revision = db.get(PromptRevision, item.revision_id) if item else None
    if revision and not revision.is_deleted:
        return revision.content
    fallback = db.scalar(select(PromptRevision).where(PromptRevision.template_key == key, PromptRevision.is_deleted.is_(False)).order_by(PromptRevision.number.desc()).limit(1))
    return fallback.content if fallback else DEFAULTS[key]


def get_active_prompt(key: str) -> str:
    from wellflow.app.database import session_scope
    try:
        with session_scope() as db:
            return active_prompt(db, key)
    except Exception:
        return DEFAULTS[key]
