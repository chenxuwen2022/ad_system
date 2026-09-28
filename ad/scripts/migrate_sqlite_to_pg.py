# -*- coding: utf-8 -*-
"""一次性迁移：把旧 SQLite (ad/token_store.db) 里的业务数据迁到远程 PostgreSQL。

用法：
    .venv\\Scripts\\python.exe migrate_sqlite_to_pg.py

说明：
- 只做一次，跑完即可删除
- 已存在的记录（按唯一键冲突）会跳过，不会覆盖 PG 新数据
- material_tag 旧表无 tag_type 列，默认补 'public'
"""
import os
import sqlite3
import sys

from sqlalchemy import create_engine, text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SQLITE_PATH = os.path.join(BASE_DIR, "ad", "token_store.db")
PG_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgresSY123456@192.168.110.254:5432/wellflow",
)


# 每张表：(表名, 旧 SQLite 列清单, 新 PG 列清单, 唯一冲突处理)
# 顺序很重要：先迁父表再迁子表（这里无外键依赖，顺序不严格）
TABLES = [
    {
        "table": "douyin_token",
        "sqlite_cols": ["id", "access_token", "refresh_token", "expires_in", "refresh_expires_in", "update_time"],
        "pg_cols":    ["access_token", "refresh_token", "expires_in", "refresh_expires_in", "update_time"],
        "conflict": "ON CONSTRAINT douyin_token_pkey DO NOTHING",
    },
    {
        "table": "advertiser_account",
        "sqlite_cols": ["id", "advertiser_id", "name", "update_time"],
        "pg_cols":    ["advertiser_id", "name", "update_time"],
        "conflict": "ON CONFLICT (advertiser_id) DO NOTHING",
    },
    {
        "table": "material_tag",
        "sqlite_cols": ["id", "name", "color", "create_time", "update_time"],
        "pg_cols":    ["name", "color", "tag_type", "create_time", "update_time"],
        "extra_values": {"tag_type": "public"},   # 旧表无此列，补默认
        "conflict": "ON CONFLICT (name) DO NOTHING",
    },
    {
        "table": "material_category",
        "sqlite_cols": ["id", "name", "level", "parent_id", "sort", "create_time", "update_time"],
        "pg_cols":    ["name", "level", "parent_id", "sort", "create_time", "update_time"],
        "conflict": "DO NOTHING",
    },
    {
        "table": "material_mark",
        "sqlite_cols": ["id", "file_path", "tags", "create_time", "update_time"],
        "pg_cols":    ["file_path", "tags", "create_time", "update_time"],
        "conflict": "ON CONFLICT (file_path) DO NOTHING",
    },
    {
        "table": "material_launch",
        "sqlite_cols": ["id", "file_path", "status", "mode", "biz_status", "plan_id", "plan_name", "product_id", "detail", "create_time"],
        "pg_cols":    ["file_path", "status", "mode", "biz_status", "plan_id", "plan_name", "product_id", "detail", "create_time"],
        "conflict": "DO NOTHING",
    },
]


def sqlite_table_exists(conn, table):
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def migrate_table(pg_engine, spec):
    table = spec["table"]
    sql_cols = spec["sqlite_cols"]
    pg_cols = spec["pg_cols"]

    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if not sqlite_table_exists(conn, table):
            print(f"[skip] 旧库无表 {table}")
            return
        # 实际读取时按旧表真实列名取（兼容旧表缺列）
        cur = conn.execute(f"SELECT * FROM {table}")
        rows = cur.fetchall()
        if not rows:
            print(f"[empty] {table} 无数据")
            return

        n_ok = 0
        n_skip = 0
        for row in rows:
            data = dict(row)
            vals = []
            for c in pg_cols:
                if c in spec.get("extra_values", {}):
                    vals.append(spec["extra_values"][c])
                else:
                    vals.append(data.get(c))

            col_list = ", ".join(pg_cols)
            placeholders = ", ".join([f":{i}" for i in range(len(pg_cols))])
            conflict = spec["conflict"]
            sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT {conflict[16:] if conflict.startswith('ON CONFLICT') else conflict}"
            # 简化：直接拼
            if conflict == "DO NOTHING":
                sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
            elif conflict.startswith("ON CONSTRAINT"):
                sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT {conflict}"
            else:
                sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) {conflict}"

            params = {str(i): v for i, v in enumerate(vals)}
            try:
                with pg_engine.begin() as pg_conn:
                    pg_conn.execute(text(sql), params)
                n_ok += 1
            except Exception as e:
                n_skip += 1
        print(f"[ok]   {table}: 新增 {n_ok} 行，跳过 {n_skip} 行")
    finally:
        conn.close()


def main():
    if not os.path.isfile(SQLITE_PATH):
        print(f"找不到旧 SQLite 文件: {SQLITE_PATH}")
        sys.exit(1)
    print(f"旧库: {SQLITE_PATH}")
    print(f"新库: {PG_URL}")
    print("-" * 50)

    pg_engine = create_engine(PG_URL, pool_pre_ping=True)
    for spec in TABLES:
        try:
            migrate_table(pg_engine, spec)
        except Exception as e:
            print(f"[err]  {spec['table']}: {e}")
    print("-" * 50)
    print("迁移完成。")


if __name__ == "__main__":
    main()
