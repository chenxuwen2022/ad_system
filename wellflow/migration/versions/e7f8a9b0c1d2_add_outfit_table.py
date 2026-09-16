"""add outfit table

Revision ID: e7f8a9b0c1d2
Revises: b2c3d4e5f6a7
Create Date: 2026-09-16 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "outfit",
        sa.Column("id", sa.BIGINT(), autoincrement=True, nullable=False),
        sa.Column("outfit_no", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("desc", sa.Text(), nullable=True),
        sa.Column("tags", sa.String(length=512), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="mine"),
        sa.Column("origin", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("cover_storage_uri", sa.String(length=512), nullable=True),
        sa.Column("original_storage_uri", sa.String(length=512), nullable=True),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("dims", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("owner_id", sa.BIGINT(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("outfit_no"),
    )
    op.create_index("ix_outfit_scope", "outfit", ["scope", "status"], unique=False)
    op.create_index("ix_outfit_no", "outfit", ["outfit_no"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_outfit_no", table_name="outfit")
    op.drop_index("ix_outfit_scope", table_name="outfit")
    op.drop_table("outfit")
