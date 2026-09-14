"""add task_image table

Revision ID: c3d4a1b2e5f7
Revises: 5efd8dd13e9a
Create Date: 2026-09-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4a1b2e5f7'
down_revision: Union[str, Sequence[str], None] = '5efd8dd13e9a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('task_image',
        sa.Column('image_id', sa.String(length=64), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=False),
        sa.Column('image_type', sa.String(length=16), nullable=False),
        sa.Column('storage_uri', sa.String(length=512), nullable=False),
        sa.Column('shot_id', sa.String(length=64), nullable=True),
        sa.Column('prompt', sa.Text(), nullable=True),
        sa.Column('prompt_index', sa.Integer(), nullable=True),
        sa.Column('variant_index', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['task.task_id'], ),
        sa.PrimaryKeyConstraint('image_id')
    )
    op.create_index(op.f('ix_task_image_task_id'), 'task_image', ['task_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_task_image_task_id'), table_name='task_image')
    op.drop_table('task_image')