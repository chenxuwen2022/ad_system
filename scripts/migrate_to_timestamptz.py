"""一次性时间列迁移脚本。

把 PG 里所有 wellflow 表的 timestamp without time zone 列转成
timestamp with time zone (timestamptz)，存量数据按 UTC 解释（因为
历史上全部是 datetime.utcnow 写进去的，值本身就是 UTC 时刻）。

安全：USING <col> AT TIME ZONE 'UTC' 会把现有 naive 字符串贴上 UTC，
      然后 PG 内部按 UTC 存成 timestamptz，读回来不会有数值偏移。
      等价于 "在原时刻上 + 补上 UTC 时区信息"。

只动 wellflow 表（由模型定义的那 10 张业务表）。
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgresSY123456@localhost:5432/wellflow",
)

ALTER_COLUMNS = [
    # conversation / chat_message
    ("conversation", "created_at"),
    ("conversation", "updated_at"),
    ("chat_message", "created_at"),
    # task + 子表
    ("task", "created_at"),
    ("task", "updated_at"),
    ("task_event", "created_at"),
    ("task_error_log", "created_at"),
    ("task_image", "created_at"),
    # mannequin
    ("mannequin", "created_at"),
    ("mannequin", "updated_at"),
    ("mannequin_generate_log", "created_at"),
    # product
    ("product_brand", "created_at"),
    ("product_brand", "updated_at"),
    ("product_series", "created_at"),
    ("product_series", "updated_at"),
    ("product_sku", "created_at"),
    ("product_sku", "updated_at"),
    ("product_image", "created_at"),
    ("product_historical_asset", "created_at"),
    ("product_knowledge_link", "linked_at"),
]


def main() -> None:
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # 1) 检查列当前类型
    print("=== BEFORE ===")
    cur.execute("""
        SELECT table_name, column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema='public' AND udt_name IN ('timestamp','timestamptz')
        ORDER BY table_name, column_name
    """)
    for r in cur.fetchall():
        print(f"  {r['table_name']:<28} {r['column_name']:<14} {r['data_type']}")

    print(f"\n=== ALTER {len(ALTER_COLUMNS)} columns ===")
    for table, col in ALTER_COLUMNS:
        sql = f"""
            ALTER TABLE {table}
            ALTER COLUMN {col}
            TYPE timestamptz
            USING {col} AT TIME ZONE 'UTC'
        """
        cur.execute(sql)
        print(f"  OK  {table}.{col}")

    # 2) 验证新旧数据都是 timestamptz，值不变
    print("\n=== AFTER sample ===")
    cur.execute("""
        SELECT 'conversation' AS t, conversation_id,
               created_at AT TIME ZONE 'UTC' AS created_at_utc,
               updated_at AT TIME ZONE 'UTC' AS updated_at_utc,
               pg_typeof(created_at) AS created_at_type
        FROM conversation
        ORDER BY created_at DESC LIMIT 3
    """)
    for r in cur.fetchall():
        print(f"  {r}")

    cur.execute("""
        SELECT column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema='public' AND udt_name IN ('timestamp','timestamptz')
        ORDER BY table_name, column_name
    """)
    still_naive = [r for r in cur.fetchall() if r["udt_name"] == "timestamp"]
    if still_naive:
        print("\n⚠️ 还有 timestamp without time zone 没转完:")
        for r in still_naive:
            print(f"  {r['table_name']}.{r['column_name']}")
    else:
        print("\n✅ 所有时间列都已转成 timestamptz")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
