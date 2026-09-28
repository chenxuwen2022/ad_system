import sys
sys.path.insert(0, '.')

from ad.db import SessionLocal, MaterialLaunchDB

db = SessionLocal()
try:
    # 统计删除前的数量
    count_before = db.query(MaterialLaunchDB).count()
    print(f"删除前素材记录数量: {count_before}")
    
    # 删除所有素材记录
    db.query(MaterialLaunchDB).delete()
    db.commit()
    
    # 统计删除后的数量
    count_after = db.query(MaterialLaunchDB).count()
    print(f"删除后素材记录数量: {count_after}")
    print("✅ 素材记录已删除")
finally:
    db.close()
