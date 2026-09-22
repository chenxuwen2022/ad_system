# -*- coding: utf-8 -*-
"""场景库建表迁移(挂在 add_conversation_id_short 之后)。"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a5b6c7d8e9f0"
down_revision: Union[str, Sequence[str], None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scene",
        sa.Column("id", sa.BIGINT(), primary_key=True, autoincrement=True),
        sa.Column("scene_no", sa.String(64), unique=True, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("desc", sa.Text(), nullable=True),
        sa.Column("tags", sa.String(512), nullable=True),
        sa.Column("scope", sa.String(16), nullable=False, server_default="mine"),
        sa.Column("origin", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="extracting"),
        sa.Column("cover_storage_uri", sa.String(512), nullable=True),
        sa.Column("original_storage_uri", sa.String(512), nullable=True),
        sa.Column("dims", postgresql.JSONB(), nullable=True),
        sa.Column("owner_id", sa.BIGINT(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_scene_scope", "scene", ["scope", "status"])
    op.create_index("ix_scene_no", "scene", ["scene_no"])


def downgrade() -> None:
    op.drop_index("ix_scene_no", table_name="scene")
    op.drop_index("ix_scene_scope", table_name="scene")
    op.drop_table("scene")
