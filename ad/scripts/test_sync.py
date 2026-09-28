import sys
sys.path.insert(0, ".")

from ad.db import SessionLocal, MaterialLaunchDB
from ad.douyin_api import DouYinAdService
import requests, json

db = SessionLocal()
try:
    # 查询所有有 material_id 的投放记录
    rows = db.query(MaterialLaunchDB).filter(
        MaterialLaunchDB.material_id != "",
        MaterialLaunchDB.mode == "real"
    ).all()
    
    print(f"找到 {len(rows)} 条需要同步的记录")
    
    for row in rows:
        print(f"\n记录: {row.file_path}")
        print(f"  material_id: {row.material_id}")
        print(f"  advertiser_id: {row.advertiser_id}")
        print(f"  当前状态: {row.biz_status}")
        
        # 调用千川接口查询素材状态
        try:
            ad_service = DouYinAdService(advertiser_id=row.advertiser_id)
            mtype = "视频" if row.detail and "video" in row.detail.lower() else "图片"
            path = "/open_api/v1.0/qianchuan/image/get/" if mtype == "图片" else "/open_api/v1.0/qianchuan/video/get/"
            
            print(f"  调用接口: {path}")
            rj = requests.get(
                f"{ad_service.base_url}{path}",
                headers=ad_service.headers,
                params={
                    "advertiser_id": int(row.advertiser_id),
                    "filtering": json.dumps({"material_ids": [int(row.material_id)]})
                },
                timeout=30
            ).json()
            
            print(f"  接口返回 code: {rj.get('code')}")
            print(f"  接口返回 msg: {rj.get('message')}")
            
            if rj.get("code") == 0:
                lst = rj.get("data", {}).get("list", []) or []
                print(f"  返回素材数量: {len(lst)}")
                if lst:
                    print(f"  审核状态: {lst[0].get('audit_status')}")
        except Exception as e:
            print(f"  错误: {e}")
finally:
    db.close()
