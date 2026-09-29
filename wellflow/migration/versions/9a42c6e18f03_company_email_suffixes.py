"""Company email suffixes and company-targeted operation logs.

Revision ID: 9a42c6e18f03
Revises: d6e7f8a9b0c1
"""
from alembic import op
import sqlalchemy as sa

revision = "9a42c6e18f03"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("companies", sa.Column("email_suffixes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    op.alter_column("operation_logs", "target_user_id", existing_type=sa.Integer(), nullable=True)
    op.alter_column("operation_logs", "target_username", existing_type=sa.String(100), type_=sa.String(200))


def downgrade():
    # Company log entries have no user target; refuse destructive downgrade.
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM operation_logs WHERE target_user_id IS NULL OR length(target_username) > 100")):
        raise RuntimeError("已有公司操作日志，不能无损回退此迁移")
    op.alter_column("operation_logs", "target_username", existing_type=sa.String(200), type_=sa.String(100))
    op.alter_column("operation_logs", "target_user_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column("companies", "email_suffixes")
