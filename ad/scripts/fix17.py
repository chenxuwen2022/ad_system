file_path = r"ad\routes\ad_routes.py"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 找到同步状态的逻辑，修改成用计划详情接口
old = """                # 判断 material_id 是数字格式还是字符串格式
                if material_id.isdigit():
                    # 数字格式，直接查询
                    rj = requests.get(
                        f"{ad_service.base_url}{path}",
                        headers=ad_service.headers,
                        params={
                            "advertiser_id": int(aid),
                            "filtering": json.dumps({"material_ids": [int(material_id)]})
                        },
                        timeout=30
                    ).json()
                else:
                    # 字符串格式（素材库 ID），用素材库接口查询
                    try:
                        lib_info = ad_service._fetch_material_lib(material_id, mtype)
                        if lib_info:
                            rj = {"code": 0, "data": {"list": [lib_info]}}
                        else:
                            rj = {"code": -1, "message": "素材库信息为空"}
                    except Exception as e:
                        rj = {"code": -1, "message": str(e)}"""

new = """                # 直接用计划详情接口判断素材状态
                try:
                    detail = ad_service.get_overall_plan_detail(row.plan_id)
                    creatives = detail.get("multi_product_creative_list", []) or []
                    
                    # 找到对应的素材
                    found = False
                    for c in creatives:
                        imgs = c.get("image_material", []) or []
                        vids = c.get("video_material", []) or []
                        
                        for im in imgs:
                            ids = im.get("image_ids", []) or []
                            if material_id in [str(i) for i in ids]:
                                found = True
                                break
                        for v in vids:
                            if str(v.get("video_id", "")) == material_id:
                                found = True
                                break
                        
                        if found:
                            break
                    
                    # 如果在计划里找到了素材，说明已经投放成功，审核通过
                    if found:
                        rj = {"code": 0, "data": {"list": [{"audit_status": "AUDIT_STATUS_APPROVED"}]}}
                    else:
                        rj = {"code": -1, "message": "在计划里找不到这个素材"}
                except Exception as e:
                    rj = {"code": -1, "message": str(e)}"""

if old in content:
    content = content.replace(old, new)
    print("✅ 修改同步状态逻辑成功")
else:
    print("❌ 没找到要修改的内容")

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

print("完成")
