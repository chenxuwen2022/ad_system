import sys
sys.path.insert(0, ".")

from ad.db import SessionLocal, MaterialLaunchDB

db = SessionLocal()
try:
    rows = db.query(MaterialLaunchDB).all()
    
    print(f"总共 {len(rows)} 条投放记录")
    print()
    
    for row in rows:
        print(f"文件: {row.file_path}")
        print(f"  当前业务状态: {row.biz_status}")
        print()
finally:
    db.close()
