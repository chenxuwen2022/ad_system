import sys
sys.path.insert(0, ".")

from ad.db import engine, SessionLocal, MaterialLaunchDB

print(f"数据库连接: {engine.url}")
print()

# 查询一下数据量
db = SessionLocal()
try:
    rows = db.query(MaterialLaunchDB).all()
    print(f"MaterialLaunchDB 表有 {len(rows)} 条记录")
    
    from collections import Counter
    advertiser_counts = Counter(row.advertiser_id for row in rows)
    
    print()
    print("按 advertiser_id 分组:")
    for aid, count in advertiser_counts.items():
        print(f"  {aid}: {count} 条")
finally:
    db.close()
