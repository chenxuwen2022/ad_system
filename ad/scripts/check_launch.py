import sys
sys.path.insert(0, '.')

from ad.db import SessionLocal, MaterialLaunchDB

db = SessionLocal()
try:
    rows = db.query(MaterialLaunchDB).all()
    print(f"数据库里一共有 {len(rows)} 条投放记录")
    print("-" * 80)
    for r in rows:
        print(f"ID: {r.id}")
        print(f"文件: {r.file_path}")
        print(f"状态: {r.status}")
        print(f"业务状态: {r.biz_status}")
        print(f"计划ID: {r.plan_id}")
        print(f"计划名: {r.plan_name}")
        print(f"素材ID: {r.material_id}")
        print(f"错误: {r.detail}")
        print(f"时间: {r.create_time}")
        print("-" * 80)
finally:
    db.close()
