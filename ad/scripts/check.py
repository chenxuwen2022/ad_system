import sys
sys.path.insert(0, ".")
import os
from ad.db import SessionLocal, MaterialLaunchDB

db = SessionLocal()
try:
    rows = db.query(MaterialLaunchDB).all()
    print("数据库里的记录:")
    for r in rows:
        print(f"  - {r.file_path}")
    
    print()
    print("上传目录里的文件:")
    upload_dir = "ad/media_storage"
    for f in os.listdir(upload_dir):
        print(f"  - {f}")
finally:
    db.close()
