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
        print(f"  当前状态: {row.biz_status}")
        print(f"  material_id: {row.material_id}")
        print()
        
        # 如果是待审核，改成已投放
        if row.biz_status == "待审核" or row.biz_status == "审核中":
            row.biz_status = "已投放"
            print(f"  ✅ 已更新为: 已投放")
    
    db.commit()
    print("✅ 完成")
finally:
    db.close()
