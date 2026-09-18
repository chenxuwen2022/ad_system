"""add conversation_id_short to conversation

Revision ID: c9d0e1f2a3b4
Revises: e7f8a9b0c1d2
Create Date: 2026-09-17 22:30:00.000000

模型在 0e615d9 加了 Conversation.conversation_id_short（String(12), NOT NULL, unique），
但当时漏写了迁移，导致已部署环境的 conversation 表缺该列、列表接口 500。
本迁移补列并按 ConversationRepo._short_of 的规则（去掉 '-' 取前 12 位）回填存量数据。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) 先加可空列
    op.add_column(
        "conversation",
        sa.Column("conversation_id_short", sa.String(length=12), nullable=True),
    )

    # 2) 回填：与 ConversationRepo._short_of 规则一致（去 '-' 后取前 12 位）
    op.execute(
        "UPDATE conversation "
        "SET conversation_id_short = left(replace(conversation_id, '-', ''), 12)"
    )

    # 3) 唯一约束前防御：若回填后出现重复短 ID，直接报错并列出冲突值，人工处理
    conn = op.get_bind()
    duplicates = conn.execute(
        sa.text(
            "SELECT conversation_id_short, count(*) AS cnt "
            "FROM conversation GROUP BY conversation_id_short HAVING count(*) > 1"
        )
    ).fetchall()
    if duplicates:
        raise RuntimeError(
            f"conversation_id_short 回填后存在重复值，无法加唯一约束: {duplicates}"
        )

    # 4) 补齐约束：NOT NULL + UNIQUE（对齐模型定义）
    op.alter_column("conversation", "conversation_id_short", nullable=False)
    op.create_unique_constraint(
        "uq_conversation_conversation_id_short",
        "conversation",
        ["conversation_id_short"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_conversation_conversation_id_short", "conversation", type_="unique"
    )
    op.drop_column("conversation", "conversation_id_short")
