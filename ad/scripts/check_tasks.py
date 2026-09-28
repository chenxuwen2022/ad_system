import sys
sys.path.insert(0, ".")

from ad.db import SessionLocal, MaterialLaunchDB

db = SessionLocal()
try:
    rows = db.query(MaterialLaunchDB).all()
    
    print(f"数据库里总共 {len(rows)} 条投放记录")
    print()
    
    # 按 advertiser_id 分组统计
    from collections import Counter
    advertiser_counts = Counter(row.advertiser_id for row in rows)
    
    print("按 advertiser_id 分组:")
    for aid, count in advertiser_counts.items():
        print(f"  {aid}: {count} 条")
finally:
    db.close()
