"""archive prompts without breaking release history

Revision ID: a5e9c1d2f3b4
Revises: f4c2e8a1d9b0
"""
from alembic import op
import sqlalchemy as sa

revision = "a5e9c1d2f3b4"
down_revision = "f4c2e8a1d9b0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("prompt_template", sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("prompt_template", "is_archived")
