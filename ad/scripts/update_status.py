import sys
sys.path.insert(0, ".")

from ad.db import SessionLocal, MaterialLaunchDB

db = SessionLocal()
try:
    rows = db.query(MaterialLaunchDB).all()
    
    print(f"总共 {len(rows)} 条投放记录")
    
    for row in rows:
        print(f"\n文件: {row.file_path}")
        print(f"  修改前状态: {row.biz_status}")
        
        # 全部改成已投放
        row.biz_status = "已投放"
        
        print(f"  修改后状态: {row.biz_status}")
    
    db.commit()
    print("\n✅ 完成，所有素材状态已更新为已投放")
finally:
    db.close()
