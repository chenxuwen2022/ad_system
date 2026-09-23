# -*- coding: utf-8 -*-
"""场景库对齐产品 demo:① scene 表加马赛克原图列 ② 取消审核环节(存量状态映射)。"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b5c6d7e8f9a0"
down_revision: Union[str, Sequence[str], None] = "f1e2d3c4b5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ① 原图马赛克版(人+违规物打码):「查看原图」切换的数据源
    op.add_column(
        "scene",
        sa.Column("mosaic_storage_uri", sa.String(512), nullable=True),
    )

    # ② 取消审核环节:存量 pending_review/approved/returned 全部转 active(直接可用)
    op.execute(
        sa.text(
            "UPDATE scene SET status = 'active' "
            "WHERE status IN ('pending_review', 'approved', 'returned')"
        )
    )


def downgrade() -> None:
    # 状态映射不可逆(无法区分原 pending_review/approved/returned),仅删列
    op.drop_column("scene", "mosaic_storage_uri")
