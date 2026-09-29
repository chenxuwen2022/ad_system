"""Seed root super-admin and backfill legacy rows under 数语深流.

Purpose:
  - fresh deploy: ensure the '数语深流' company exists with allow_platform_admin=TRUE
    and a 'root' platform_admin account is always available (password from
    AUTH_INITIAL_PASSWORD env var, default 'sy123456').
  - existing DB: keep company/root idempotent (ON CONFLICT DO NOTHING), then
    backfill any legacy rows where company_id or owner_id are NULL under root.

Idempotent — safe to re-run on an already-seeded DB.
"""
import os
from datetime import datetime, timezone

import bcrypt
from alembic import op
import sqlalchemy as sa

revision = '20260929_seed_root'
down_revision = 'ef826cd5ba64'
branch_labels = None
depends_on = None

COMPANY_NAME = '数语深流'
COMPANY_EMAIL_SUFFIXES = ['@wellflowtech.cn']
ROOT_USERNAME = 'root'
ROOT_EMAIL = 'root@wellflowtech.cn'
ROOT_ROLE = 'platform_admin'

TABLES = (
    'conversation', 'task', 'product_brand', 'product_series', 'product_sku',
    'mannequin', 'scene', 'outfit', 'mannequin_generate_log', 'async_task',
)


def upgrade():
    conn = op.get_bind()

    email_suffixes_sql = f"ARRAY[{', '.join(repr(s) for s in COMPANY_EMAIL_SUFFIXES)}]::text[]"

    # --- 1. 确保公司存在并允许平台超管 ---
    # 公司唯一性约束是带 WHERE deleted_at IS NULL 的 partial unique index，
    # 直接用 INSERT ... ON CONFLICT (lower(trim(name))) 会匹配不到约束，
    # 因此这里用 SELECT-then-INSERT 模式保证幂等。
    existing = conn.execute(
        sa.text("SELECT id FROM companies WHERE name = :name ORDER BY id LIMIT 1"),
        {'name': COMPANY_NAME},
    ).mappings().first()
    if existing is None:
        conn.execute(
            sa.text(
                f"""INSERT INTO companies (name, email_suffixes, allow_platform_admin)
                    VALUES (:name, {email_suffixes_sql}, TRUE)"""
            ),
            {'name': COMPANY_NAME},
        )
        existing = conn.execute(
            sa.text("SELECT id FROM companies WHERE name = :name ORDER BY id LIMIT 1"),
            {'name': COMPANY_NAME},
        ).mappings().first()
    else:
        conn.execute(
            sa.text(
                f"""UPDATE companies
                    SET allow_platform_admin = TRUE,
                        deleted_at = NULL,
                        email_suffixes = {email_suffixes_sql}
                    WHERE id = :id"""
            ),
            {'id': existing['id']},
        )
    if existing is None:
        raise RuntimeError(f'无法创建或查找公司「{COMPANY_NAME}」')
    company_id = existing['id']
    print(f'[seed_root] company_id = {company_id}')

    # --- 2. 确保 root 超管账号存在（密码走 AUTH_INITIAL_PASSWORD） ---
    password = os.environ.get('AUTH_INITIAL_PASSWORD', 'sy123456')
    if len(password) > 72:
        raise ValueError('AUTH_INITIAL_PASSWORD 超过 bcrypt 72 字节上限')
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(rounds=12)).decode('ascii')
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        sa.text(
            """
            INSERT INTO users (company_id, email, username, password_hash, role, created_at, updated_at)
            VALUES (:company_id, :email, :username, :password_hash, :role, :now, :now)
            ON CONFLICT (email) DO UPDATE
              SET company_id    = EXCLUDED.company_id,
                  username      = EXCLUDED.username,
                  role          = EXCLUDED.role,
                  password_hash = EXCLUDED.password_hash,
                  deleted_at    = NULL,
                  updated_at    = :now
            """
        ),
        {
            'company_id': company_id,
            'email': ROOT_EMAIL,
            'username': ROOT_USERNAME,
            'password_hash': hashed,
            'role': ROOT_ROLE,
            'now': now,
        },
    )
    user = conn.execute(
        sa.text(
            "SELECT id FROM users WHERE username = :username AND company_id = :company_id AND deleted_at IS NULL LIMIT 1"
        ),
        {'username': ROOT_USERNAME, 'company_id': company_id},
    ).mappings().first()
    if user is None:
        raise RuntimeError(f'无法创建或查找用户「{ROOT_USERNAME}」')
    user_id = user['id']
    print(f'[seed_root] user_id = {user_id} (role={ROOT_ROLE})')

    # --- 3. 回填历史数据 owner_id / company_id ---
    backfilled_any = False
    for table in TABLES:
        try:
            count = conn.execute(sa.text(f'SELECT COUNT(*) FROM {table}')).scalar_one()
        except Exception:
            continue  # 表不存在 / 不可访问，跳过
        if not count:
            continue
        # 仅填补空的 owner_id / company_id，不覆盖已有归属
        conn.execute(
            sa.text(
                f"UPDATE {table} "
                f"SET company_id = COALESCE(company_id, :company_id), "
                f"    owner_id   = COALESCE(owner_id,   :user_id) "
                f"WHERE company_id IS NULL OR owner_id IS NULL"
            ),
            {'company_id': company_id, 'user_id': user_id},
        )
        new_count = conn.execute(
            sa.text(f'SELECT COUNT(*) FROM {table} WHERE company_id IS NULL OR owner_id IS NULL')
        ).scalar_one()
        print(f'[seed_root] {table}: {new_count} 行仍未归属')
        backfilled_any = True

    if not backfilled_any:
        print('[seed_root] 全新部署：所有业务表均为空，跳过历史回填')
    print('[seed_root] 完成')


def downgrade():
    """不自动撤销公司和 root 账号——迁移会重新建表/删列，用户/公司表不会被删除。
    如果一定要清理，手工 SQL 执行：
        DELETE FROM users  WHERE username = :username AND email = :email;
        DELETE FROM companies WHERE name = :name;
    """
    print('[seed_root] downgrade 保留公司与 root 账号，请按需手工删除')
