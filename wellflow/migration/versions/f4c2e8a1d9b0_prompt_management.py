"""prompt management

Revision ID: f4c2e8a1d9b0
Revises: b2c3d4e5f6a7, d8e9f0a1b2c3
"""
from alembic import op
import sqlalchemy as sa

revision = "f4c2e8a1d9b0"
down_revision = ("b2c3d4e5f6a7", "d8e9f0a1b2c3")
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("prompt_template",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("draft_revision_id", sa.Integer(), nullable=True))
    op.create_index("ix_prompt_template_category", "prompt_template", ["category"])
    op.create_table("prompt_revision",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("template_key", sa.String(80), sa.ForeignKey("prompt_template.key"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("note", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("template_key", "number"))
    op.create_index("ix_prompt_revision_template_key", "prompt_revision", ["template_key"])
    op.create_table("prompt_release",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("category", "number"))
    op.create_index("ix_prompt_release_category", "prompt_release", ["category"])
    op.create_table("prompt_release_item",
        sa.Column("release_id", sa.Integer(), sa.ForeignKey("prompt_release.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("template_key", sa.String(80), sa.ForeignKey("prompt_template.key"), primary_key=True),
        sa.Column("revision_id", sa.Integer(), sa.ForeignKey("prompt_revision.id"), nullable=False))


def downgrade():
    op.drop_table("prompt_release_item")
    op.drop_index("ix_prompt_release_category", "prompt_release")
    op.drop_table("prompt_release")
    op.drop_index("ix_prompt_revision_template_key", "prompt_revision")
    op.drop_table("prompt_revision")
    op.drop_index("ix_prompt_template_category", "prompt_template")
    op.drop_table("prompt_template")
