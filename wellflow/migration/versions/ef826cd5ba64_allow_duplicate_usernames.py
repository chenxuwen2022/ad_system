"""Allow duplicate usernames; email remains the unique login identifier."""
from alembic import op
import sqlalchemy as sa

revision = "ef826cd5ba64"
down_revision = "de604ab3f842"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("uq_users_company_username", "users", type_="unique")


def downgrade():
    duplicate = op.get_bind().execute(sa.text(
        "SELECT company_id, username FROM users "
        "GROUP BY company_id, username HAVING count(*) > 1 LIMIT 1"
    )).first()
    if duplicate:
        raise RuntimeError("Cannot restore company username uniqueness while duplicates exist")
    op.create_unique_constraint("uq_users_company_username", "users", ["company_id", "username"])
