"""soft delete prompt revisions while retaining release history

Revision ID: c7d4a8e2b9f0
Revises: a5e9c1d2f3b4
"""
from alembic import op
import sqlalchemy as sa

revision = "c7d4a8e2b9f0"
down_revision = "a5e9c1d2f3b4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("prompt_revision", sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("prompt_revision", "is_deleted")
