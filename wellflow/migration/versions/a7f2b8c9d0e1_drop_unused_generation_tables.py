"""drop unused generation domain tables

删除未被生图主流程使用的 4 张表：
  - generation_work_item
  - generation_attempt
  - qa_result
  - sku_output_image

work_items 已持久化在 LangGraph checkpoint 中，成品图写入 task_image 表。

Revision ID: a7f2b8c9d0e1
Revises: c3d4a1b2e5f7
Create Date: 2026-09-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7f2b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'c3d4a1b2e5f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """按外键依赖逆序 DROP TABLE（CASCADE 处理 FK 引用）。"""
    # 1. 最叶子：qa_result（依赖 generation_attempt + generation_work_item）
    op.drop_index(op.f('ix_qa_result_work_item_id'), table_name='qa_result', if_exists=True)
    op.drop_table('qa_result', if_exists=True)

    # 2. generation_attempt（依赖 generation_work_item）
    op.drop_index(op.f('ix_generation_attempt_work_item_id'), table_name='generation_attempt', if_exists=True)
    op.drop_table('generation_attempt', if_exists=True)

    # 3. sku_output_image（依赖 generation_work_item）
    op.drop_index(op.f('ix_sku_output_image_task_id'), table_name='sku_output_image', if_exists=True)
    op.drop_table('sku_output_image', if_exists=True)

    # 4. 根：generation_work_item
    op.drop_index(op.f('ix_generation_work_item_task_id'), table_name='generation_work_item', if_exists=True)
    op.drop_table('generation_work_item', if_exists=True)


def downgrade() -> None:
    """回滚：重建 4 张表。

    注：此 downgrade 仅用于开发/测试环境回滚，线上一般不执行。
    """
    # 1. 先建根表 generation_work_item
    op.create_table('generation_work_item',
        sa.Column('work_item_id', sa.String(length=64), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=False),
        sa.Column('plan_id', sa.String(length=64), nullable=False),
        sa.Column('shot_id', sa.String(length=64), nullable=False),
        sa.Column('skill', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='pending'),
        sa.Column('prompt_snapshot', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['task.task_id'], ),
        sa.PrimaryKeyConstraint('work_item_id'),
    )
    op.create_index(op.f('ix_generation_work_item_task_id'), 'generation_work_item', ['task_id'], unique=False)

    # 2. generation_attempt
    op.create_table('generation_attempt',
        sa.Column('attempt_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('work_item_id', sa.String(length=64), nullable=False),
        sa.Column('attempt_index', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('provider', sa.String(length=64), nullable=True),
        sa.Column('model', sa.String(length=64), nullable=True),
        sa.Column('returncode', sa.Integer(), nullable=True),
        sa.Column('manifest_json', sa.JSON(), nullable=True),
        sa.Column('cost_usd', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['work_item_id'], ['generation_work_item.work_item_id'], ),
        sa.PrimaryKeyConstraint('attempt_id'),
    )
    op.create_index(op.f('ix_generation_attempt_work_item_id'), 'generation_attempt', ['work_item_id'], unique=False)

    # 3. sku_output_image
    op.create_table('sku_output_image',
        sa.Column('image_id', sa.String(length=64), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=False),
        sa.Column('work_item_id', sa.String(length=64), nullable=False),
        sa.Column('storage_uri', sa.String(length=512), nullable=False),
        sa.Column('shot_id', sa.String(length=64), nullable=False),
        sa.Column('human_decision', sa.String(length=16), nullable=False, server_default='approve'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['task.task_id'], ),
        sa.ForeignKeyConstraint(['work_item_id'], ['generation_work_item.work_item_id'], ),
        sa.PrimaryKeyConstraint('image_id'),
    )
    op.create_index(op.f('ix_sku_output_image_task_id'), 'sku_output_image', ['task_id'], unique=False)

    # 4. qa_result（叶子表，依赖 generation_attempt + generation_work_item）
    op.create_table('qa_result',
        sa.Column('qa_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('work_item_id', sa.String(length=64), nullable=False),
        sa.Column('attempt_id', sa.Integer(), nullable=True),
        sa.Column('passed', sa.Boolean(), nullable=False),
        sa.Column('risk_level', sa.String(length=16), nullable=False, server_default='low'),
        sa.Column('issues_json', sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column('correction_suggestion', sa.Text(), nullable=False, server_default=''),
        sa.Column('round_index', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['attempt_id'], ['generation_attempt.attempt_id'], ),
        sa.ForeignKeyConstraint(['work_item_id'], ['generation_work_item.work_item_id'], ),
        sa.PrimaryKeyConstraint('qa_id'),
    )
    op.create_index(op.f('ix_qa_result_work_item_id'), 'qa_result', ['work_item_id'], unique=False)
