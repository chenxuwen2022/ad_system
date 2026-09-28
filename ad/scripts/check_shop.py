import sys
sys.path.insert(0, ".")

from ad.db import SessionLocal, MaterialLaunchDB

db = SessionLocal()
try:
    # 先查询有多少条记录
    rows = db.query(MaterialLaunchDB).all()
    
    print(f"总共 {len(rows)} 条投放记录")
    print()
    
    # 看看每条记录的字段，找到店铺相关的字段
    for row in rows[:3]:
        print(f"文件: {row.file_path}")
        print(f"  所有字段: {row.__dict__}")
        print()
finally:
    db.close()
