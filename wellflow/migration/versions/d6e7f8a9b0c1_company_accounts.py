"""Company accounts and administrator operation logs.

Revision ID: d6e7f8a9b0c1
Revises: b7c8d9e0f1a2
"""
from alembic import op
import sqlalchemy as sa

revision = "d6e7f8a9b0c1"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("companies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("phone", sa.String(40), nullable=True),
        sa.Column("password_hash", sa.String(100), nullable=False),
        sa.Column("role", sa.String(30), nullable=False, server_default="company_user"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.CheckConstraint("role IN ('platform_admin', 'company_admin', 'company_user')", name="ck_users_role"))
    op.create_index("ix_users_company_id", "users", ["company_id"])
    op.create_table("operation_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("operator_username", sa.String(100), nullable=False),
        sa.Column("target_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("target_username", sa.String(100), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_operation_logs_company_time", "operation_logs", ["company_id", "created_at"])
    company = sa.table("companies", sa.column("name", sa.String()))
    op.bulk_insert(company, [{"name": "数语深流"}])


def downgrade():
    op.drop_table("operation_logs")
    op.drop_table("users")
    op.drop_table("companies")
