"""SKU image types and conversation binding.

Revision ID: f2a6b8d9e0c1
Revises: e8b5f3c1a6d2
"""
from alembic import op
import sqlalchemy as sa

revision = "f2a6b8d9e0c1"
down_revision = "e8b5f3c1a6d2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("product_image", sa.Column("image_type", sa.String(16), nullable=False, server_default="product"))
    op.add_column("product_image", sa.Column("source_task_id", sa.String(64), nullable=True))
    op.create_check_constraint("ck_product_image_type", "product_image", "image_type IN ('product', 'ad')")
    op.create_index("ix_product_image_type", "product_image", ["sku_id", "image_type"])
    op.add_column("conversation", sa.Column("sku_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key("fk_conversation_sku", "conversation", "product_sku", ["sku_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_conversation_sku_id", "conversation", ["sku_id"])


def downgrade():
    op.drop_index("ix_conversation_sku_id", table_name="conversation")
    op.drop_constraint("fk_conversation_sku", "conversation", type_="foreignkey")
    op.drop_column("conversation", "sku_id")
    op.drop_index("ix_product_image_type", table_name="product_image")
    op.drop_constraint("ck_product_image_type", "product_image", type_="check")
    op.drop_column("product_image", "source_task_id")
    op.drop_column("product_image", "image_type")
