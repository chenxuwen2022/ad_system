"""Published system prompts are loaded exclusively from the database."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from wellflow.app.models.prompt_models import PromptRelease, PromptReleaseItem, PromptRevision, PromptTemplate

CATEGORIES = {
    "commerce": "商拍提示词",
    "refine": "微调提示词",
    "intent": "意图识别提示词",
    "mannequin": "模特库提示词",
    "outfit": "穿搭库提示词",
    "scene": "场景库提示词",
}
CATALOG = (
    ("product_report", "commerce", "生产品报告"),
    ("shoot_plan", "commerce", "生商拍策划"),
    ("image_prompt", "commerce", "生图提示词"),
    ("refine_report", "refine", "产品报告微调"),
    ("refine_plan", "refine", "商拍策划微调"),
    ("refine_image_prompt", "refine", "生图提示词微调"),
    ("intent_classifier", "intent", "意图识别"),
    ("mannequin_optimize", "mannequin", "提示词优化"),
    ("mannequin_auto_tag", "mannequin", "自动打标"),
    ("mannequin_fine_tune", "mannequin", "生图微调-身份维持"),
    ("outfit_recognize", "outfit", "单品识别"),
    ("outfit_cutout", "outfit", "单品抠图"),
    ("outfit_auto_tag", "outfit", "自动打标"),
    ("scene_extract", "scene", "场景提取"),
    ("scene_mosaic", "scene", "原图马赛克"),
    ("scene_auto_tag", "scene", "自动打标"),
)


class PromptUnavailableError(RuntimeError):
    """The requested system prompt has no usable published database revision."""


def active_prompt(db: Session, key: str) -> str:
    template = db.get(PromptTemplate, key)
    if template is None or template.is_archived:
        raise PromptUnavailableError(f"提示词 {key} 尚未在数据库初始化")
    release = db.scalar(select(PromptRelease).where(
        PromptRelease.category == template.category,
    ).order_by(PromptRelease.number.desc()).limit(1))
    if release is None:
        raise PromptUnavailableError(f"提示词 {key} 所属分类尚未发布")
    item = db.get(PromptReleaseItem, (release.id, key))
    if item is None:
        raise PromptUnavailableError(f"提示词 {key} 不在当前发布版本中")
    revision = db.get(PromptRevision, item.revision_id)
    if revision is None or revision.is_deleted or revision.template_key != key:
        raise PromptUnavailableError(f"提示词 {key} 的已发布修订不可用")
    return revision.content


def get_active_prompt(key: str) -> str:
    from wellflow.app.database import session_scope

    with session_scope() as db:
        return active_prompt(db, key)
