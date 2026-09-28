import sys
sys.path.insert(0, ".")

from ad.db import SessionLocal, MaterialLaunchDB
from ad.douyin_api import DouYinAdService
import requests, json

db = SessionLocal()
try:
    rows = db.query(MaterialLaunchDB).all()
    
    print(f"总共 {len(rows)} 条投放记录")
    print()
    
    for row in rows:
        print(f"文件: {row.file_path}")
        print(f"  当前业务状态: {row.biz_status}")
        print(f"  material_id: {row.material_id}")
        print(f"  plan_id: {row.plan_id}")
        print()
        
        # 查询千川素材状态
        if row.material_id:
            try:
                svc = DouYinAdService(advertiser_id=row.advertiser_id)
                mtype = "图片"
                material_id = row.material_id
                
                # 判断格式
                if material_id.isdigit():
                    print(f"  数字格式，直接查询")
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
                            print(f"  千川审核状态: {audit_status}")
                            
                            # 更新状态
                            if audit_status == "AUDIT_STATUS_APPROVED":
                                row.biz_status = "已投放"
                                print(f"  ✅ 更新为: 已投放")
                            elif audit_status == "AUDIT_STATUS_REJECTED":
                                row.biz_status = "审核驳回"
                                print(f"  ✅ 更新为: 审核驳回")
                            elif audit_status == "AUDIT_STATUS_PENDING":
                                row.biz_status = "待审核"
                                print(f"  ✅ 更新为: 待审核")
                    else:
                        print(f"  ❌ 查询失败: {rj.get('message')}")
                else:
                    print(f"  字符串格式，用素材库接口查询")
                    lib_info = svc._fetch_material_lib(material_id, mtype)
                    if lib_info:
                        audit_status = lib_info.get("audit_status", "")
                        print(f"  千川审核状态: {audit_status}")
                        
                        # 更新状态
                        if audit_status == "AUDIT_STATUS_APPROVED":
                            row.biz_status = "已投放"
                            print(f"  ✅ 更新为: 已投放")
                        elif audit_status == "AUDIT_STATUS_REJECTED":
                            row.biz_status = "审核驳回"
                            print(f"  ✅ 更新为: 审核驳回")
                        elif audit_status == "AUDIT_STATUS_PENDING":
                            row.biz_status = "待审核"
                            print(f"  ✅ 更新为: 待审核")
                    else:
                        print(f"  ❌ 素材库信息为空")
            except Exception as e:
                print(f"  ❌ 错误: {e}")
        
        print("---")
    
    db.commit()
    print("\n✅ 完成")
finally:
    db.close()
