"""SKU: style_no 改 NOT NULL + 加三元组唯一约束

Revision ID: a1b2c3d4e5f6
Revises: b2c3d4e5f6a7
Create Date: 2026-09-15 23:50:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) 把遗留的 NULL style_no 置为空串，避免 SET NOT NULL 失败
    op.execute("UPDATE product_sku SET style_no = '' WHERE style_no IS NULL")

    # 2) 去重：保留每组 (series_id, style_no, name) 最早创建的记录，删掉其余重复行
    op.execute("""
        DELETE FROM product_sku
        WHERE ctid IN (
            SELECT ctid FROM (
                SELECT ctid, row_number() OVER (
                    PARTITION BY series_id, style_no, name ORDER BY created_at
                ) AS rn FROM product_sku
            ) _dup WHERE rn > 1
        )
    """)

    # 3) style_no 改为 NOT NULL
    op.alter_column(
        "product_sku",
        "style_no",
        existing_type=sa.String(length=64),
        existing_nullable=True,
        nullable=False,
    )

    # 4) 三元组唯一约束：同系列 + 同货号 + 同商品名 不能重复
    op.create_unique_constraint(
        "uk_product_sku_series_style_name",
        "product_sku",
        ["series_id", "style_no", "name"],
    )


def downgrade() -> None:
    op.drop_constraint("uk_product_sku_series_style_name", "product_sku", type_="unique")

    op.alter_column(
        "product_sku",
        "style_no",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        nullable=True,
    )
