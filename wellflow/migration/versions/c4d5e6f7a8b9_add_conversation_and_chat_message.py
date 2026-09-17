"""add conversation + chat_message + task.conversation_id

Revision ID: c4d5e6f7a8b9
Revises: b2c3d4e5f6a7
Create Date: 2026-09-17 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # conversation
    op.create_table(
        'conversation',
        sa.Column('conversation_id', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=128), nullable=False, server_default='新对话'),
        sa.Column('current_task_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('conversation_id'),
    )
    # task.conversation_id FK
    op.add_column('task', sa.Column('conversation_id', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_task_conversation_id'), 'task', ['conversation_id'], unique=False)
    op.create_foreign_key(
        'fk_task_conversation', 'task', 'conversation',
        ['conversation_id'], ['conversation_id'], ondelete='SET NULL',
    )
    # chat_message
    op.create_table(
        'chat_message',
        sa.Column('message_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('conversation_id', sa.String(length=64), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=True),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('text', sa.Text(), nullable=False, server_default=''),
        sa.Column('images_json', sa.JSON(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('intent', sa.String(length=32), nullable=True),
        sa.Column('session_index', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['conversation_id'], ['conversation.conversation_id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['task_id'], ['task.task_id'], ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('message_id'),
    )
    op.create_index(op.f('ix_chat_message_conversation_id'), 'chat_message', ['conversation_id'], unique=False)
    op.create_index(op.f('ix_chat_message_task_id'), 'chat_message', ['task_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_chat_message_task_id'), table_name='chat_message')
    op.drop_index(op.f('ix_chat_message_conversation_id'), table_name='chat_message')
    op.drop_table('chat_message')
    op.drop_constraint('fk_task_conversation', 'task', type_='foreignkey')
    op.drop_index(op.f('ix_task_conversation_id'), table_name='task')
    op.drop_column('task', 'conversation_id')
    op.drop_table('conversation')
