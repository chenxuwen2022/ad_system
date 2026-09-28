import sys
sys.path.insert(0, ".")

from ad.db import SessionLocal, MaterialLaunchDB

db = SessionLocal()
try:
    # 先查询有多少条记录
    rows = db.query(MaterialLaunchDB).filter(
        MaterialLaunchDB.advertiser_id == "1854728640974923"
    ).all()
    
    print(f"找到 {len(rows)} 条 Babycare童装奥莱旗舰店 的投放记录")
    
    for row in rows:
        print(f"  {row.file_path}")
        print(f"    状态: {row.biz_status}")
    
    # 删除这些记录
    for row in rows:
        db.delete(row)
    
    db.commit()
    print(f"\n✅ 已删除 {len(rows)} 条记录")
finally:
    db.close()
