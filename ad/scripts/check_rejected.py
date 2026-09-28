import sys
sys.path.insert(0, '.')
from ad.db import SessionLocal, MaterialLaunchDB

db = SessionLocal()
try:
    # 查询所有素材
    rows = db.query(MaterialLaunchDB).order_by(MaterialLaunchDB.create_time.desc()).all()
    
    print(f"共 {len(rows)} 条素材记录")
    print("=" * 80)
    
    for r in rows[:20]:  # 只看前20条
        print(f"ID: {r.id}")
        print(f"文件: {r.file_path}")
        print(f"状态: {r.biz_status}")
        print(f"计划: {r.plan_name}")
        print(f"material_id: {r.material_id}")
        print(f"advertiser_id: {r.advertiser_id}")
        print(f"reject_reason: {r.reject_reason if hasattr(r, 'reject_reason') else '字段不存在'}")
        print(f"detail: {r.detail}")
        print("-" * 80)
        
finally:
    db.close()
