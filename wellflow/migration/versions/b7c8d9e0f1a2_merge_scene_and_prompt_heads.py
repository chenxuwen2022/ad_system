# -*- coding: utf-8 -*-
"""合并两条迁移链:场景库链(b5c6d7e8f9a0)与 master 提示词/SKU 链(f2a6b8d9e0c1)。"""

from typing import Sequence, Union

from alembic import op

revision: str = "b7c8d9e0f1a2"
# 合并两个 head:我们的场景库链 + master 新迁移链
down_revision: Union[str, Sequence[str], None] = ("b5c6d7e8f9a0", "f2a6b8d9e0c1")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
