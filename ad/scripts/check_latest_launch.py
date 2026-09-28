import sys
sys.path.insert(0, '.')
from ad.db import SessionLocal, MaterialLaunchDB

db = SessionLocal()
try:
    # 查询最新的素材记录
    rows = db.query(MaterialLaunchDB).order_by(MaterialLaunchDB.create_time.desc()).limit(10).all()
    
    print(f"最新 {len(rows)} 条素材记录")
    print("=" * 80)
    
    for r in rows:
        print(f"ID: {r.id}")
        print(f"文件: {r.file_path}")
        print(f"状态: {r.biz_status}")
        print(f"计划: {r.plan_name} (ID: {r.plan_id})")
        print(f"material_id: {r.material_id}")
        print(f"advertiser_id: {r.advertiser_id}")
        print(f"reject_reason: {r.reject_reason if hasattr(r, 'reject_reason') else '字段不存在'}")
        print(f"detail: {r.detail}")
        print(f"创建时间: {r.create_time}")
        print("-" * 80)
        
finally:
    db.close()
