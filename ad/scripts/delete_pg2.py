import sys
sys.path.insert(0, ".")

from ad.pg_db import PG_SessionLocal, LaunchRecordDB

db = PG_SessionLocal()
try:
    # 先查询有多少条记录
    rows = db.query(LaunchRecordDB).all()
    
    print(f"PostgreSQL 数据库里总共 {len(rows)} 条投放记录")
    print()
    
    # 按 advertiser_id 分组统计
    from collections import Counter
    advertiser_counts = Counter(row.advertiser_id for row in rows)
    
    print("按 advertiser_id 分组:")
    for aid, count in advertiser_counts.items():
        print(f"  {aid}: {count} 条")
    
    print()
    
    # 删除 Babycare童装奥莱旗舰店 的数据（advertiser_id: 1854728640974923）
    babycare_rows = db.query(LaunchRecordDB).filter(
        LaunchRecordDB.advertiser_id == "1854728640974923"
    ).all()
    
    print(f"找到 {len(babycare_rows)} 条 Babycare童装奥莱旗舰店 的记录")
    
    for row in babycare_rows:
        print(f"  {row.file_path}")
        db.delete(row)
    
    db.commit()
    print(f"\n✅ 已删除 {len(babycare_rows)} 条记录")
    
    # 确认删除后的数量
    remaining = db.query(LaunchRecordDB).all()
    print(f"\n删除后剩余 {len(remaining)} 条记录")
finally:
    db.close()
