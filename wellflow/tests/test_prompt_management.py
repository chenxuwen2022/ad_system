"""Prompt revisions remain immutable; a category release is a full snapshot."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from fastapi import HTTPException

from wellflow.app.database import Base
from wellflow.app.models.prompt_models import PromptRelease, PromptReleaseItem, PromptRevision, PromptTemplate
from wellflow.app.prompt.registry import active_prompt, seed_prompts, DEFAULTS
from wellflow.app.api.prompts import (
    PromptCreate, PromptUpdate, RevisionInput, ReleaseInput, create_prompt,
    delete_prompt, delete_revision, get_prompt, list_prompts, publish, save_revision, update_prompt,
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
        result = delete_revision("extra_prompt", updated["draft_revision_id"], db)
        assert result["release_version"] == 3
        assert get_prompt("extra_prompt", db)["draft_content"] == "初稿"
        assert list_prompts(db)[0]["items"]
        assert len(db.query(PromptReleaseItem).filter_by(release_id=db.query(PromptRelease).filter_by(category="commerce", number=3).one().id).all()) == 4
        assert db.query(PromptRevision).filter_by(template_key="extra_prompt").count() == 2
        assert len(db.query(PromptReleaseItem).filter_by(release_id=published["id"]).all()) == 4
        try:
            delete_prompt("extra_prompt", db)
            assert False, "last revision must be protected"
        except HTTPException as exc:
            assert exc.status_code == 409
        assert get_prompt("extra_prompt", db)["draft_content"] == "初稿"


def test_intent_v2_delete_falls_back_to_v1_and_cannot_delete_last():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        PromptTemplate.__table__, PromptRevision.__table__, PromptRelease.__table__, PromptReleaseItem.__table__,
    ])
    with Session(engine) as db:
        seed_prompts(db)
        v1 = get_prompt("intent_classifier", db)
        v2 = update_prompt("intent_classifier", PromptUpdate(
            name="意图识别", content="新版意图识别", expected_revision_id=v1["draft_revision_id"]), db)
        publish("intent", ReleaseInput(note="v2"), db)
        assert active_prompt(db, "intent_classifier") == "新版意图识别"
        v3 = update_prompt("intent_classifier", PromptUpdate(
            name="意图识别", content="未发布第三稿", expected_revision_id=v2["draft_revision_id"]), db)
        result = delete_revision("intent_classifier", v2["draft_revision_id"], db)
        assert result["release_version"] == 3
        assert result["active_revision_id"] == v1["draft_revision_id"]
        assert active_prompt(db, "intent_classifier") == DEFAULTS["intent_classifier"]
        assert get_prompt("intent_classifier", db)["draft_revision_id"] == v3["draft_revision_id"]
        delete_revision("intent_classifier", v3["draft_revision_id"], db)
        assert get_prompt("intent_classifier", db)["draft_revision_id"] == v1["draft_revision_id"]
        try:
            delete_revision("intent_classifier", v1["draft_revision_id"], db)
            assert False, "last revision must be protected"
        except HTTPException as exc:
            assert exc.status_code == 409
