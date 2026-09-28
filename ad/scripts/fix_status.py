import sys
sys.path.insert(0, ".")

from ad.db import SessionLocal, MaterialLaunchDB

db = SessionLocal()
try:
    rows = db.query(MaterialLaunchDB).filter(
        MaterialLaunchDB.biz_status == "通过-待投放"
    ).all()
    
    print(f"找到 {len(rows)} 条通过-待投放的记录")
    
    for row in rows:
        row.biz_status = "已投放"
        print(f"  {row.file_path}")
    
    db.commit()
    print("✅ 完成，所有通过-待投放都已更新为已投放")
finally:
    db.close()
