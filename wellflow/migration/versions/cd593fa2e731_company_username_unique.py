"""Scope usernames to company; login uses globally unique email."""
from alembic import op
import sqlalchemy as sa

revision = "cd593fa2e731"
down_revision = "bc482f91d620"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint("uq_users_company_username", "users", ["company_id", "username"])
    op.drop_constraint("uq_users_username", "users", type_="unique")


def downgrade():
    # Fail before altering constraints if cross-company duplicates now exist.
    duplicate = op.get_bind().execute(sa.text(
        "SELECT username FROM users GROUP BY username HAVING count(*) > 1 LIMIT 1"
    )).first()
    if duplicate:
        raise RuntimeError("Cannot restore globally unique usernames while duplicates exist")
    op.create_unique_constraint("uq_users_username", "users", ["username"])
    op.drop_constraint("uq_users_company_username", "users", type_="unique")
