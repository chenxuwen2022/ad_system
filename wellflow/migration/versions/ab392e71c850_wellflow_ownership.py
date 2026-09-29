"""Isolate Wellflow conversations and company assets; backfill approved owner.

Revision ID: ab392e71c850
Revises: f0c1d2e3a4b5
"""
from alembic import op
import sqlalchemy as sa

revision = 'ab392e71c850'
down_revision = 'f0c1d2e3a4b5'
branch_labels = None
depends_on = None
TABLES = ('conversation', 'task', 'product_brand', 'product_series', 'product_sku',
          'mannequin', 'scene', 'outfit', 'mannequin_generate_log', 'async_task')
EXISTING_OWNER = {'mannequin', 'scene', 'outfit'}


def upgrade():
    """加列 + FK + 索引。不再回填历史数据（列保持 nullable=True；
    fresh deploy 没数据，有历史数据则让应用层写入补齐）。"""
    for table in TABLES:
        op.add_column(table, sa.Column('company_id', sa.BigInteger(), nullable=True))
        if table not in EXISTING_OWNER:
            op.add_column(table, sa.Column('owner_id', sa.BigInteger(), nullable=True))
        for field, target in (('company_id', 'companies'), ('owner_id', 'users')):
            op.create_foreign_key(
                f'fk_{table}_{field}', table, target, [field], ['id'], ondelete='RESTRICT'
            )
        op.create_index(f'ix_{table}_company_owner', table, ['company_id', 'owner_id'])


def downgrade():
    for table in reversed(TABLES):
        op.drop_index(f'ix_{table}_company_owner', table_name=table)
        for field in ('company_id', 'owner_id'):
            op.drop_constraint(f'fk_{table}_{field}', table, type_='foreignkey')
        op.drop_column(table, 'company_id')
        if table not in EXISTING_OWNER:
            op.drop_column(table, 'owner_id')
        else:
            op.alter_column(table, 'owner_id', nullable=True)
