"""Permit physical account deletion while retaining historical IDs and logs."""
from alembic import op
import sqlalchemy as sa

revision = "de604ab3f842"
down_revision = "cd593fa2e731"
branch_labels = None
depends_on = None


def upgrade():
    # Historical owner/operator/target IDs deliberately outlive accounts.
    # Never cascade deletes into logs, conversations or assets.
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    for table in inspector.get_table_names():
        for fk in inspector.get_foreign_keys(table):
            if fk["referred_table"] == "users":
                op.drop_constraint(fk["name"], table, type_="foreignkey")
    conn.execute(sa.text("DELETE FROM users WHERE deleted_at IS NOT NULL"))


def downgrade():
    raise RuntimeError("Deleted accounts cannot be reconstructed; restore from a database backup")
