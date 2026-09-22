"""Prompt revisions remain immutable; a category release is a full snapshot."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from wellflow.app.database import Base
from wellflow.app.models.prompt_models import PromptRelease, PromptReleaseItem, PromptRevision, PromptTemplate
from wellflow.app.prompt.registry import active_prompt, seed_prompts, DEFAULTS
from wellflow.app.api.prompts import (
    PromptCreate, PromptUpdate, RevisionInput, ReleaseInput, create_prompt,
    delete_prompt, get_prompt, list_prompts, publish, save_revision, update_prompt,
)


def test_category_release_keeps_all_three_prompts_in_one_version():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        PromptTemplate.__table__, PromptRevision.__table__, PromptRelease.__table__, PromptReleaseItem.__table__,
    ])
    with Session(engine) as db:
        seed_prompts(db)
        assert active_prompt(db, "product_report") == DEFAULTS["product_report"]
        assert active_prompt(db, "shoot_plan") == DEFAULTS["shoot_plan"]
        save_revision("product_report", RevisionInput(content="新版报告", note="调整报告"), db)
        save_revision("shoot_plan", RevisionInput(content="新版策划", note="调整策划"), db)
        assert active_prompt(db, "product_report") == DEFAULTS["product_report"]
        result = publish("commerce", ReleaseInput(note="联合发布"), db)
        assert result["version"] == 2
        assert active_prompt(db, "product_report") == "新版报告"
        assert active_prompt(db, "shoot_plan") == "新版策划"
        assert active_prompt(db, "image_prompt") == DEFAULTS["image_prompt"]
        items = db.query(PromptReleaseItem).filter_by(release_id=result["id"]).all()
        assert len(items) == 3
        assert db.query(PromptRevision).filter_by(template_key="product_report").count() == 2


def test_prompt_crud_preserves_release_history():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        PromptTemplate.__table__, PromptRevision.__table__, PromptRelease.__table__, PromptReleaseItem.__table__,
    ])
    with Session(engine) as db:
        seed_prompts(db)
        created = create_prompt(PromptCreate(key="extra_prompt", category="commerce", name="补充提示词", content="初稿"), db)
        assert created["draft_number"] == 1
        assert get_prompt("extra_prompt", db)["draft_content"] == "初稿"
        updated = update_prompt("extra_prompt", PromptUpdate(name="新版名称", content="第二稿", expected_revision_id=created["draft_revision_id"]), db)
        assert updated["draft_number"] == 2
        assert updated["name"] == "新版名称"
        published = publish("commerce", ReleaseInput(note="新增提示词"), db)
        assert len(db.query(PromptReleaseItem).filter_by(release_id=published["id"]).all()) == 4
        delete_prompt("extra_prompt", db)
        assert all(item["key"] != "extra_prompt" for item in list_prompts(db)[0]["items"])
        assert list_prompts(db)[0]["changed"] is True
        after_delete = publish("commerce", ReleaseInput(note="删除提示词"), db)
        assert len(db.query(PromptReleaseItem).filter_by(release_id=after_delete["id"]).all()) == 3
        assert db.query(PromptRevision).filter_by(template_key="extra_prompt").count() == 2
        assert len(db.query(PromptReleaseItem).filter_by(release_id=published["id"]).all()) == 4
