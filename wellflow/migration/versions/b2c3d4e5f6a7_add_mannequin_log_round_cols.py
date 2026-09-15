"""add round_type / parent_log_id / target_image_uri to mannequin_generate_log

Revision ID: b2c3d4e5f6a7
Revises: d9e0f1a2b3c4
Create Date: 2026-09-15 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "d9e0f1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """给 mannequin_generate_log 追加 3 列，支持首轮 + 微调多轮日志追踪。"""

    # round_type —— first_round / fine_tune
    op.add_column(
        "mannequin_generate_log",
        sa.Column(
            "round_type", sa.String(length=32), nullable=False,
            server_default="first_round",
        ),
    )
    # parent_log_id —— 微调记录指向父级首轮记录
    op.add_column(
        "mannequin_generate_log",
        sa.Column("parent_log_id", sa.BIGINT(), nullable=True),
    )
    # target_image_uri —— 微调时选中的那张图
    op.add_column(
        "mannequin_generate_log",
        sa.Column("target_image_uri", sa.String(length=512), nullable=True),
    )

    # FK + 索引
    op.create_foreign_key(
        "fk_mannequin_gen_parent",
        "mannequin_generate_log", "mannequin_generate_log",
        ["parent_log_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_mannequin_gen_parent",
        "mannequin_generate_log", ["parent_log_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_mannequin_gen_parent", table_name="mannequin_generate_log")
    op.drop_constraint("fk_mannequin_gen_parent", "mannequin_generate_log", type_="foreignkey")
    op.drop_column("mannequin_generate_log", "target_image_uri")
    op.drop_column("mannequin_generate_log", "parent_log_id")
    op.drop_column("mannequin_generate_log", "round_type")
