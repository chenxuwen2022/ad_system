import sys
sys.path.insert(0, ".")
from ad.db import SessionLocal, MaterialLaunchDB

db = SessionLocal()
try:
    rows = db.query(MaterialLaunchDB).order_by(MaterialLaunchDB.create_time.desc()).all()
    print(f"当前有 {len(rows)} 条记录")
    # 保留最新的 1 条，删除其他的
    for r in rows[1:]:
        print(f"删除 ID={r.id}: {r.file_path}")
        db.delete(r)
    db.commit()
    
    # 更新最新那条的状态为"已投放"
    if rows:
        latest = rows[0]
        latest.biz_status = "已投放"
        latest.status = "success"
        db.commit()
        print(f"更新 ID={latest.id} 状态为已投放")
finally:
    db.close()
