"""One operation log per batch, with tenant-scoped target snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "6f02a4b19c87"
down_revision = "9a42c6e18f03"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("operation_logs", sa.Column("targets", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM operation_logs WHERE json_array_length(targets) > 0")):
        raise RuntimeError("已有批量操作日志，不能无损回退此迁移")
    op.drop_column("operation_logs", "targets")
