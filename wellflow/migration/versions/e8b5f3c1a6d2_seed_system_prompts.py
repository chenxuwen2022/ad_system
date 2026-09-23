"""Seed seven system prompts from a fixed migration snapshot.

Revision ID: e8b5f3c1a6d2
Revises: c7d4a8e2b9f0
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "e8b5f3c1a6d2"
down_revision = "c7d4a8e2b9f0"
branch_labels = None
depends_on = None

CATALOG = (
    ("product_report", "commerce", "生产品报告"),
    ("shoot_plan", "commerce", "生商拍策划"),
    ("image_prompt", "commerce", "生图提示词"),
    ("refine_report", "refine", "产品报告微调"),
    ("refine_plan", "refine", "商拍策划微调"),
    ("refine_image_prompt", "refine", "生图提示词微调"),
    ("intent_classifier", "intent", "意图识别"),
)


def upgrade():
    db = op.get_bind()
    seeds = json.loads((Path(__file__).resolve().parents[1] / "prompt_seed_v1.json").read_text(encoding="utf-8"))
    assert {key for key, _, _ in CATALOG} == set(seeds), "提示词初始数据不完整"
    existing = set(db.execute(sa.text("SELECT key FROM prompt_template")).scalars())
    required = {key for key, _, _ in CATALOG}
    if required <= existing:
        return  # 已由旧版本管理接口初始化；保留现有编辑和发布历史
    if existing:
        raise RuntimeError(f"提示词数据只初始化了一部分，缺少: {sorted(required - existing)}")

    now = datetime.now(timezone.utc)
    for category in ("commerce", "refine", "intent"):
        release_id = db.execute(sa.text("""
            INSERT INTO prompt_release (category, number, note, created_at)
            VALUES (:category, 1, :note, :created_at) RETURNING id
        """), {"category": category, "note": "初始版本", "created_at": now}).scalar_one()
        for key, item_category, name in CATALOG:
            if item_category != category:
                continue
            db.execute(sa.text("""
                INSERT INTO prompt_template (key, category, name, draft_revision_id, is_archived)
                VALUES (:key, :category, :name, NULL, false)
            """), {"key": key, "category": category, "name": name})
            revision_id = db.execute(sa.text("""
                INSERT INTO prompt_revision (template_key, number, content, note, created_at, is_deleted)
                VALUES (:key, 1, :content, :note, :created_at, false) RETURNING id
            """), {"key": key, "content": seeds[key], "note": "初始版本", "created_at": now}).scalar_one()
            db.execute(sa.text("UPDATE prompt_template SET draft_revision_id = :revision_id WHERE key = :key"),
                {"revision_id": revision_id, "key": key})
            db.execute(sa.text("""
                INSERT INTO prompt_release_item (release_id, template_key, revision_id)
                VALUES (:release_id, :key, :revision_id)
            """), {"release_id": release_id, "key": key, "revision_id": revision_id})


def downgrade():
    # Data and later edits are user-owned. Downgrade must not erase them.
    pass
