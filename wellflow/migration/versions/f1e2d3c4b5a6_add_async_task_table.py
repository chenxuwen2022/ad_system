# -*- coding: utf-8 -*-
"""任务状态入 DB:async_task 表(三库异步任务统一状态表)+ 合并两个迁移头。"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f1e2d3c4b5a6"
# 合并两条分支:a5b6c7d8e9f0(场景库表)与 d8e9f0a1b2c3(同事 drop index)
down_revision: Union[str, Sequence[str], None] = ("a5b6c7d8e9f0", "d8e9f0a1b2c3")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "async_task",
        sa.Column("id", sa.BIGINT(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(64), unique=True, nullable=False),
        sa.Column("task_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="processing"),
        sa.Column("step", sa.String(32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),   # 完整任务数据(结果/进度等)
        sa.Column("row_kind", sa.String(16), nullable=True),       # outfit_id/scene_id/mannequin_id
        sa.Column("row_id", sa.BIGINT(), nullable=True),           # 关联的业务行 id
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_async_task_status", "async_task", ["status"])
    op.create_index("ix_async_task_heartbeat", "async_task", ["heartbeat_at"])
    op.create_index("ix_async_task_row", "async_task", ["row_kind", "row_id"])


def downgrade() -> None:
    op.drop_index("ix_async_task_row", table_name="async_task")
    op.drop_index("ix_async_task_heartbeat", table_name="async_task")
    op.drop_index("ix_async_task_status", table_name="async_task")
    op.drop_table("async_task")
