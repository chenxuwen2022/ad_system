import sys
sys.path.insert(0, ".")

from ad.db import SessionLocal, MaterialLaunchDB

db = SessionLocal()
try:
    rows = db.query(MaterialLaunchDB).all()
    
    print(f"总共 {len(rows)} 条投放记录")
    
    for row in rows:
        print(f"\n文件: {row.file_path}")
        print(f"  业务状态: {row.biz_status}")
        print(f"  material_id: '{row.material_id}'")
        print(f"  plan_id: {row.plan_id}")
        print(f"  advertiser_id: {row.advertiser_id}")
finally:
    db.close()
