"""drop task_image.variant_index column（Node4 已改回一 prompt 一 API 调用，
variant_index 永远为 0，彻底失去区分语义）。

Revision ID: d8e9f0a1b2c3
Revises: c9d0e1f2a3b4
Create Date: 2026-09-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8e9f0a1b2c3'
down_revision: Union[str, Sequence[str], None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema：task_image 表删 variant_index 列。"""
    op.drop_column('task_image', 'variant_index')


def downgrade() -> None:
    """Downgrade schema：task_image 表加回 variant_index 列（nullable，保留旧语义）。"""
    op.add_column(
        'task_image',
        sa.Column('variant_index', sa.Integer(), nullable=True),
    )
