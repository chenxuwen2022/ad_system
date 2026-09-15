"""add product (brand/series/sku) and reference mannequin tables

Revision ID: d9e0f1a2b3c4
Revises: f8a1b2c3d4e5
Create Date: 2026-09-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d9e0f1a2b3c4"
down_revision: Union[str, Sequence[str], None] = "f8a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # ============ 品牌 ============
    op.create_table(
        "product_brand",
        sa.Column("id", sa.BIGINT(), autoincrement=True, nullable=False),
        sa.Column("brand_no", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("brand_guide", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("series_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sku_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("brand_no"),
    )
    op.create_index("ix_product_brand_status", "product_brand", ["status"], unique=False)

    # ============ 系列 ============
    op.create_table(
        "product_series",
        sa.Column("id", sa.BIGINT(), autoincrement=True, nullable=False),
        sa.Column("brand_id", sa.BIGINT(), nullable=False),
        sa.Column("series_no", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("sku_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["brand_id"], ["product_brand.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_no"),
    )
    op.create_index("ix_product_series_brand", "product_series", ["brand_id"], unique=False)

    # ============ SKU 主表 ============
    op.create_table(
        "product_sku",
        sa.Column("id", sa.BIGINT(), autoincrement=True, nullable=False),
        sa.Column("sku_no", sa.String(length=64), nullable=False),
        sa.Column("series_id", sa.BIGINT(), nullable=False),
        sa.Column("brand_id", sa.BIGINT(), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("style_no", sa.String(length=64), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("color", sa.String(length=32), nullable=True),
        sa.Column("material", sa.String(length=64), nullable=True),
        sa.Column("silhouette", sa.String(length=32), nullable=True),
        sa.Column("season", sa.String(length=32), nullable=True),
        sa.Column("selling_points", sa.Text(), nullable=True),
        sa.Column("brand_summary", sa.Text(), nullable=True),
        sa.Column("source_url", sa.String(length=1024), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["series_id"], ["product_series.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["brand_id"], ["product_brand.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sku_no"),
    )
    op.create_index("ix_product_sku_series", "product_sku", ["series_id"], unique=False)
    op.create_index("ix_product_sku_brand", "product_sku", ["brand_id"], unique=False)
    op.create_index("ix_product_sku_status", "product_sku", ["status"], unique=False)

    # ============ SKU 图片 ============
    op.create_table(
        "product_image",
        sa.Column("id", sa.BIGINT(), autoincrement=True, nullable=False),
        sa.Column("sku_id", sa.BIGINT(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("storage_uri", sa.String(length=512), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["sku_id"], ["product_sku.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_image_sku", "product_image", ["sku_id"], unique=False)
    op.create_index("ix_product_image_cat", "product_image", ["sku_id", "category"], unique=False)

    # ============ SKU 历史素材 ============
    op.create_table(
        "product_historical_asset",
        sa.Column("id", sa.BIGINT(), autoincrement=True, nullable=False),
        sa.Column("sku_id", sa.BIGINT(), nullable=False),
        sa.Column("asset_type", sa.String(length=32), nullable=False),
        sa.Column("storage_uri", sa.String(length=512), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["sku_id"], ["product_sku.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_hist_asset_sku", "product_historical_asset", ["sku_id", "asset_type"], unique=False)

    # ============ SKU ↔ 知识库 ============
    op.create_table(
        "product_knowledge_link",
        sa.Column("id", sa.BIGINT(), autoincrement=True, nullable=False),
        sa.Column("sku_id", sa.BIGINT(), nullable=False),
        sa.Column("doc_type", sa.String(length=16), nullable=False),
        sa.Column("doc_ref", sa.String(length=256), nullable=False),
        sa.Column("match_score", sa.Integer(), nullable=True),
        sa.Column("linked_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["sku_id"], ["product_sku.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sku_id", "doc_type", "doc_ref", name="uk_product_knowledge_link"),
    )
    op.create_index("ix_product_knowledge_sku", "product_knowledge_link", ["sku_id"], unique=False)

    # ============ 模特主表 ============
    op.create_table(
        "mannequin",
        sa.Column("id", sa.BIGINT(), autoincrement=True, nullable=False),
        sa.Column("mannequin_no", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("en_name", sa.String(length=128), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="mine"),
        sa.Column("origin", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("cover_storage_uri", sa.String(length=512), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("generate_model", sa.String(length=64), nullable=True),
        sa.Column("generate_prompt", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.BIGINT(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mannequin_no"),
    )
    op.create_index("ix_mannequin_scope", "mannequin", ["scope", "status"], unique=False)
    op.create_index("ix_mannequin_owner", "mannequin", ["owner_id"], unique=False)

    # ============ 模特标签 ============
    op.create_table(
        "mannequin_tag",
        sa.Column("id", sa.BIGINT(), autoincrement=True, nullable=False),
        sa.Column("mannequin_id", sa.BIGINT(), nullable=False),
        sa.Column("group_key", sa.String(length=32), nullable=False),
        sa.Column("dim_key", sa.String(length=32), nullable=False),
        sa.Column("tag_value", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["mannequin_id"], ["mannequin.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mannequin_id", "dim_key", "tag_value", name="uk_mannequin_tag"),
    )
    op.create_index("ix_mannequin_tag_id", "mannequin_tag", ["mannequin_id"], unique=False)
    op.create_index("ix_mannequin_tag_filter", "mannequin_tag", ["dim_key", "tag_value"], unique=False)
    op.create_index("ix_mannequin_tag_group", "mannequin_tag", ["group_key"], unique=False)

    # ============ 模特 AI 生成日志 ============
    op.create_table(
        "mannequin_generate_log",
        sa.Column("id", sa.BIGINT(), autoincrement=True, nullable=False),
        sa.Column("mannequin_id", sa.BIGINT(), nullable=True),
        sa.Column("input_desc", sa.Text(), nullable=True),
        sa.Column("input_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("final_prompt", sa.Text(), nullable=True),
        sa.Column("generate_model", sa.String(length=64), nullable=True),
        sa.Column("num_output", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("output_uris", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_msg", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["mannequin_id"], ["mannequin.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mannequin_gen_id", "mannequin_generate_log", ["mannequin_id"], unique=False)
    op.create_index("ix_mannequin_gen_time", "mannequin_generate_log", ["created_at"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_mannequin_gen_time", table_name="mannequin_generate_log")
    op.drop_index("ix_mannequin_gen_id", table_name="mannequin_generate_log")
    op.drop_table("mannequin_generate_log")

    op.drop_index("ix_mannequin_tag_group", table_name="mannequin_tag")
    op.drop_index("ix_mannequin_tag_filter", table_name="mannequin_tag")
    op.drop_index("ix_mannequin_tag_id", table_name="mannequin_tag")
    op.drop_table("mannequin_tag")

    op.drop_index("ix_mannequin_owner", table_name="mannequin")
    op.drop_index("ix_mannequin_scope", table_name="mannequin")
    op.drop_table("mannequin")

    op.drop_index("ix_product_knowledge_sku", table_name="product_knowledge_link")
    op.drop_table("product_knowledge_link")

    op.drop_index("ix_product_hist_asset_sku", table_name="product_historical_asset")
    op.drop_table("product_historical_asset")

    op.drop_index("ix_product_image_cat", table_name="product_image")
    op.drop_index("ix_product_image_sku", table_name="product_image")
    op.drop_table("product_image")

    op.drop_index("ix_product_sku_status", table_name="product_sku")
    op.drop_index("ix_product_sku_brand", table_name="product_sku")
    op.drop_index("ix_product_sku_series", table_name="product_sku")
    op.drop_table("product_sku")

    op.drop_index("ix_product_series_brand", table_name="product_series")
    op.drop_table("product_series")

    op.drop_index("ix_product_brand_status", table_name="product_brand")
    op.drop_table("product_brand")
