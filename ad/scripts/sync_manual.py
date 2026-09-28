import sys
sys.path.insert(0, ".")

from ad.db import SessionLocal, MaterialLaunchDB
from ad.douyin_api import DouYinAdService
import requests, json

db = SessionLocal()
try:
    # 查询所有投放记录
    rows = db.query(MaterialLaunchDB).all()
    
    for row in rows:
        print(f"\n文件: {row.file_path}")
        print(f"  plan_id: {row.plan_id}")
        print(f"  advertiser_id: {row.advertiser_id}")
        
        if not row.plan_id or not row.advertiser_id:
            print("  跳过：没有 plan_id 或 advertiser_id")
            continue
        
        # 获取计划详情，找到素材
        try:
            svc = DouYinAdService(advertiser_id=row.advertiser_id)
            detail = svc.get_overall_plan_detail(row.plan_id)
            
            # 从计划详情里找素材
            creatives = detail.get("multi_product_creative_list", []) or []
            print(f"  找到 {len(creatives)} 个创意")
            
            # 找图片素材
            material_id = ""
            for c in creatives:
                imgs = c.get("image_material", []) or []
                if imgs:
                    ids = imgs[0].get("image_ids", []) or []
                    if ids:
                        material_id = str(ids[0])
                        print(f"  找到图片素材 ID: {material_id}")
                        break
            
            if not material_id:
                # 找视频素材
                for c in creatives:
                    vids = c.get("video_material", []) or []
                    if vids:
                        material_id = str(vids[0].get("video_id", ""))
                        print(f"  找到视频素材 ID: {material_id}")
                        break
            
            if material_id:
                # 更新数据库
                row.material_id = material_id
                print(f"  已更新 material_id: {material_id}")
                
                # 查询素材审核状态
                try:
                    rj = requests.get(
                        f"{svc.base_url}/open_api/v1.0/qianchuan/image/get/",
                        headers=svc.headers,
                        params={
                            "advertiser_id": int(row.advertiser_id),
                            "filtering": json.dumps({"material_ids": [int(material_id)]})
                        },
                        timeout=30
                    ).json()
                    
                    if rj.get("code") == 0:
                        lst = rj.get("data", {}).get("list", []) or []
                        if lst:
                            audit_status = lst[0].get("audit_status", "")
                            print(f"  审核状态: {audit_status}")
                            
                            # 更新业务状态
                            if audit_status == "AUDIT_STATUS_APPROVED":
                                row.biz_status = "已投放"
                                print(f"  更新业务状态: 已投放")
                            elif audit_status == "AUDIT_STATUS_REJECTED":
                                row.biz_status = "审核驳回"
                                print(f"  更新业务状态: 审核驳回")
                            elif audit_status == "AUDIT_STATUS_PENDING":
                                row.biz_status = "待审核"
                                print(f"  更新业务状态: 待审核")
                except Exception as e:
                    print(f"  查询审核状态失败: {e}")
            
        except Exception as e:
            print(f"  查询计划详情失败: {e}")
    
    db.commit()
    print("\n✅ 完成")
finally:
    db.close()
