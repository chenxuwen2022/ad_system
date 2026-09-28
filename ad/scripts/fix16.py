file_path = r"ad\routes\ad_routes.py"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 修改同步状态的逻辑，支持字符串格式的 material_id
old = """                # 调用千川接口查询素材状态
                import requests, json
                path = "/open_api/v1.0/qianchuan/image/get/" if mtype == "图片" else "/open_api/v1.0/qianchuan/video/get/"
                
                rj = requests.get(
                    f"{ad_service.base_url}{path}",
                    headers=ad_service.headers,
                    params={
                        "advertiser_id": int(aid),
                        "filtering": json.dumps({"material_ids": [int(material_id)]})
                    },
                    timeout=30
                ).json()"""

new = """                # 调用千川接口查询素材状态
                import requests, json
                path = "/open_api/v1.0/qianchuan/image/get/" if mtype == "图片" else "/open_api/v1.0/qianchuan/video/get/"
                
                # 判断 material_id 是数字格式还是字符串格式
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

if old in content:
    content = content.replace(old, new)
    print("✅ 修改同步状态逻辑成功")
else:
    print("❌ 没找到要修改的内容")

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

print("完成")
