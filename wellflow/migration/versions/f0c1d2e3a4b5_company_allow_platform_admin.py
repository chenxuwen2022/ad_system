"""Allow selected companies to host platform_admin users.

Revision ID: f0c1d2e3a4b5
Revises: 28c9a1e6b504
"""
from alembic import op
import sqlalchemy as sa

revision = "f0c1d2e3a4b5"
down_revision = "28c9a1e6b504"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "companies",
        sa.Column(
            "allow_platform_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE companies SET allow_platform_admin = TRUE WHERE name = '数语深流' AND deleted_at IS NULL"
        )
    )


def downgrade():
    op.drop_column("companies", "allow_platform_admin")
