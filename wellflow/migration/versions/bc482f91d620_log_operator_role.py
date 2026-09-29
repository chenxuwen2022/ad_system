"""Snapshot operator role for operation log visibility.

Revision ID: bc482f91d620
Revises: ab392e71c850
"""
from alembic import op
import sqlalchemy as sa
revision = 'bc482f91d620'
down_revision = 'ab392e71c850'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('operation_logs', sa.Column('operator_role', sa.String(30), nullable=True))
    # Historical rows lack role snapshots. Conservatively hide all history of
    # operators known to be/have been platform admins, including batch changes.
    op.execute(sa.text("""
        UPDATE operation_logs l SET operator_role = CASE
          WHEN u.role = 'platform_admin' OR EXISTS (
            SELECT 1 FROM operation_logs changes
            WHERE (changes.target_user_id = u.id OR EXISTS (
                SELECT 1 FROM json_array_elements(changes.targets) t
                WHERE (t->>'id')::integer = u.id
            )) AND (changes.detail LIKE '%平台超管%' OR changes.detail LIKE '%platform_admin%'
              OR EXISTS (SELECT 1 FROM json_array_elements(changes.targets) t
                         WHERE (t->>'id')::integer = u.id
                           AND (t->>'detail' LIKE '%平台超管%' OR t->>'detail' LIKE '%platform_admin%')))
          ) THEN 'platform_admin' ELSE 'company_admin' END
        FROM users u WHERE l.operator_id = u.id
    """))


def downgrade():
    op.drop_column('operation_logs', 'operator_role')
