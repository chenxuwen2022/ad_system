"""Soft-archive the two legacy outfit recognize prompt keys that were merged
into a single `outfit_recognize` in f3a7c9e1b5d2.

Idempotent — 已清理的数据库上再跑不会报错。

Revision ID: c1b8d4a2f7e3
Revises: f3a7c9e1b5d2
"""

from alembic import op
import sqlalchemy as sa

revision = "c1b8d4a2f7e3"
down_revision = "f3a7c9e1b5d2"
branch_labels = None
depends_on = None

_LEGACY_KEYS = ("outfit_recognize_system", "outfit_recognize_user")


def upgrade():
    conn = op.get_bind()

    for key in _LEGACY_KEYS:
        conn.execute(sa.text(
            "DELETE FROM prompt_release_item WHERE template_key = :key"
        ), {"key": key})
        conn.execute(sa.text(
            "UPDATE prompt_template SET is_archived = true WHERE key = :key"
        ), {"key": key})


def downgrade():
    pass  # 软归档保留历史 revision，不回滚。