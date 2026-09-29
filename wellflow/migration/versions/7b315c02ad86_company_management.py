"""Company management and preserved company deletion history."""
from alembic import op
import sqlalchemy as sa

revision = "7b315c02ad86"
down_revision = "6f02a4b19c87"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("companies", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("uq_companies_active_name", "companies", [sa.text("lower(trim(name))")], unique=True,
                    postgresql_where=sa.text("deleted_at IS NULL"))


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM companies WHERE deleted_at IS NOT NULL")):
        raise RuntimeError("已有已删除公司，不能无损回退此迁移")
    op.drop_index("uq_companies_active_name", table_name="companies")
    op.drop_column("companies", "deleted_at")
