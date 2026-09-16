# -*- coding: utf-8 -*-
"""一次性导入:assets_data/library.json 的 outfit 记录 → PostgreSQL outfit 表。

用法(在 wellflow 目录,用项目 venv 执行):
    python scripts/import_outfits_to_db.py

幂等:已存在的 outfit_no 自动跳过。
旧记录维度是旧口径(风格/季节/适用场景),与新六组口径不匹配,dims 留空。
"""

import json
import os
import sys

# 仓库根 = 本文件上两级(wellflow/scripts/ -> wellflow/ -> 仓库根)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from wellflow.app.database import SessionLocal  # noqa: E402
from wellflow.app.repositories.outfit_repo import OutfitRepo  # noqa: E402


def main():
    data_file = os.path.join(REPO_ROOT, "assets_data", "library.json")
    with open(data_file, encoding="utf-8") as f:
        all_records = json.load(f)
    outfits = [r for r in all_records if r.get("category") == "outfit"]
    print(f"library.json 中 outfit 记录 {len(outfits)} 条")

    db = SessionLocal()
    repo = OutfitRepo(db)
    imported = skipped = 0
    for r in outfits:
        no = r.get("id", "")
        if repo.get_by_no(no):
            skipped += 1
            continue
        repo.create(
            name=r.get("name", ""),
            desc=r.get("desc"),
            tags=r.get("tags"),
            scope=r.get("scope", "mine"),
            origin="upload",
            items=[],
            dims={},
        )
        imported += 1
    db.commit()
    db.close()
    print(f"✅ 导入完成:新增 {imported} 条,跳过已存在 {skipped} 条")


if __name__ == "__main__":
    main()
