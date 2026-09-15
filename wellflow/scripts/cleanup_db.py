"""线上数据库清理脚本。

目标：只保留 node1-node3 生图主流程用到的 4 张表，删除其他所有表。
保留表清单：
  - task               任务主表
  - task_event         任务事件审计
  - task_error_log     任务错误日志
  - task_image         任务关联图片

删除范围（资产库旧表 + 废弃生图表 + 任何其他残留表）：
  - model_asset, model_asset_tag
  - outfit_asset, outfit_asset_tag
  - background_asset, background_asset_tag
  - generation_work_item, generation_attempt, qa_result, sku_output_image
  - 任何其他表（brand / series / sku / 手动创建的测试表等·）

用法：python -m wellflow.scripts.cleanup_db [--yes]
  默认 dry-run，只打印不执行；加 --yes 才真正 DROP。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许直接 python -m wellflow.scripts.cleanup_db 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text, inspect  # noqa: E402

from wellflow.app.config import settings  # noqa: E402


# ====== 保留白名单 ======
KEEP_TABLES = {
    "task",
    "task_event",
    "task_error_log",
    "task_image",
    # alembic 版本表 —— 必须保留，否则 `alembic upgrade head` 会重新跑旧 migration
    "alembic_version",
}


def main():
    parser = argparse.ArgumentParser(description="线上数据库清理：只保留生图主流程表")
    parser.add_argument("--yes", action="store_true", help="真正执行 DROP（默认 dry-run）")
    args = parser.parse_args()

    engine = create_engine(settings.database_url)
    inspector = inspect(engine)
    all_tables = set(inspector.get_table_names())

    drop_tables = sorted(all_tables - KEEP_TABLES)
    keep_tables = sorted(all_tables & KEEP_TABLES)

    print(f"数据库: {settings.database_url}")
    print(f"\n保留的表 ({len(keep_tables)}):")
    for t in keep_tables:
        print(f"  ✅ {t}")

    print(f"\n将删除的表 ({len(drop_tables)}):")
    if not drop_tables:
        print("  （无 — 数据库已干净）")
    else:
        for t in drop_tables:
            print(f"  ❌ {t}")

    if not drop_tables:
        print("\n数据库已干净，无需清理。")
        return

    if not args.yes:
        print("\n⚠️  DRY-RUN 模式：以上表不会被删除。加 --yes 才真正执行。")
        return

    # ====== 执行 DROP ======
    with engine.begin() as conn:
        for table in drop_tables:
            # IF EXISTS 防重复执行
            conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE;'))
            print(f"  DROPPED  {table}")

    # ====== 验证 ======
    inspector2 = inspect(engine)
    remaining = sorted(set(inspector2.get_table_names()) - KEEP_TABLES)
    if remaining:
        print(f"\n⚠️  仍有 {len(remaining)} 张表未删除：{remaining}")
    else:
        print(f"\n✅ 清理完成，共删除 {len(drop_tables)} 张表。")


if __name__ == "__main__":
    main()
