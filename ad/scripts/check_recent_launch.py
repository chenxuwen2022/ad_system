import sys
sys.path.insert(0, '.')
from ad.db import SessionLocal, MaterialLaunchDB
from datetime import datetime, timedelta

db = SessionLocal()
try:
    # 查询最近1小时的记录
    one_hour_ago = datetime.now() - timedelta(hours=1)
    rows = db.query(MaterialLaunchDB).filter(
        MaterialLaunchDB.create_time >= one_hour_ago
    ).order_by(MaterialLaunchDB.create_time.desc()).all()
    
    print(f"最近1小时的投放记录：{len(rows)} 条")
    print("=" * 80)
    
    if not rows:
        print("❌ 最近1小时没有新的投放记录！")
        print("说明：投放请求可能没有到达后端，或者后端报错了。")
    else:
        for r in rows:
            print(f"ID: {r.id}")
            print(f"文件: {r.file_path}")
            print(f"状态: {r.status}")
            print(f"biz_status: {r.biz_status}")
            print(f"detail: {r.detail}")
            print(f"时间: {r.create_time}")
            print("-" * 80)
        
finally:
    db.close()
