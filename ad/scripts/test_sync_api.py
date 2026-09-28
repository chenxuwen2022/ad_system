import sys
sys.path.insert(0, ".")

from ad.db import SessionLocal, MaterialLaunchDB
from ad.douyin_api import DouYinAdService
import requests, json

db = SessionLocal()
try:
    rows = db.query(MaterialLaunchDB).filter(
        MaterialLaunchDB.material_id != ""
    ).all()
    
    print(f"找到 {len(rows)} 条有 material_id 的记录")
    
    for row in rows:
        print(f"\n文件: {row.file_path}")
        print(f"  material_id: {row.material_id}")
        print(f"  类型: {'数字' if row.material_id.isdigit() else '字符串(素材库ID)'}")
        
        # 测试两种方式
        try:
            svc = DouYinAdService(advertiser_id=row.advertiser_id)
            
            # 方式1：素材库接口
            print(f"\n  测试素材库接口...")
            lib_info = svc._fetch_material_lib(row.material_id, "图片")
            if lib_info:
                print(f"  ✅ 成功返回数据")
                print(f"  字段列表: {list(lib_info.keys())}")
                # 找审核状态相关的字段
                for k, v in lib_info.items():
                    if "audit" in k.lower() or "status" in k.lower():
                        print(f"    {k}: {v}")
            else:
                print(f"  ❌ 返回空")
                
        except Exception as e:
            print(f"  ❌ 错误: {e}")
        
        break  # 只测试第一条
finally:
    db.close()
