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


def _any_table_has_rows(conn):
    """True if at least one ownership-bearing table already has rows (legacy data)."""
    for table in TABLES:
        try:
            count = conn.execute(sa.text(f'SELECT COUNT(*) FROM {table}')).scalar_one()
        except Exception:
            continue  # 表尚不存在或不可访问，跳过
        if count:
            return True
    return False


def _apply_schema_only():
    """空库新部署：只加列 + NOT NULL + FK + 索引，跳过回填。"""
    for table in TABLES:
        op.add_column(table, sa.Column('company_id', sa.BigInteger(), nullable=True))
        if table not in EXISTING_OWNER:
            op.add_column(table, sa.Column('owner_id', sa.BigInteger(), nullable=True))
        for field, target in (('company_id', 'companies'), ('owner_id', 'users')):
            op.alter_column(table, field, nullable=False)
            op.create_foreign_key(
                f'fk_{table}_{field}', table, target, [field], ['id'], ondelete='RESTRICT'
            )
        op.create_index(f'ix_{table}_company_owner', table, ['company_id', 'owner_id'])


def _apply_with_backfill(conn):
    """带历史数据：加列 + 回填 sunzhaoye/数语深流 作为 owner + NOT NULL + FK + 索引。"""
    rows = conn.execute(
        sa.text("""
            SELECT u.id AS user_id, c.id AS company_id FROM users u
            JOIN companies c ON c.id = u.company_id
            WHERE u.username = 'sunzhaoye' AND c.name = '数语深流'
              AND u.deleted_at IS NULL AND c.deleted_at IS NULL
        """)
    ).mappings().all()
    if len(rows) != 1:
        raise RuntimeError('历史数据迁移需要唯一的数语深流公司及其 sunzhaoye 账号')
    owner = dict(rows[0])
    for table in TABLES:
        op.add_column(table, sa.Column('company_id', sa.BigInteger(), nullable=True))
        if table not in EXISTING_OWNER:
            op.add_column(table, sa.Column('owner_id', sa.BigInteger(), nullable=True))
        conn.execute(
            sa.text(f'UPDATE {table} SET company_id=:company_id, owner_id=:user_id'), owner
        )
        for field, target in (('company_id', 'companies'), ('owner_id', 'users')):
            op.alter_column(table, field, nullable=False)
            op.create_foreign_key(
                f'fk_{table}_{field}', table, target, [field], ['id'], ondelete='RESTRICT'
            )
        op.create_index(f'ix_{table}_company_owner', table, ['company_id', 'owner_id'])


def upgrade():
    conn = op.get_bind()
    if not _any_table_has_rows(conn):
        print('[ab392e71c850] 全新部署：业务表均为空，跳过 sunzhaoye 历史数据回填')
        _apply_schema_only()
        return
    _apply_with_backfill(conn)


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
