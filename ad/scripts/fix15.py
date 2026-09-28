# 1. 修改 add_video_to_plan 方法，让它返回 material_id
file_path = r"ad\douyin_api.py"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 找到方法定义，修改返回值
old = """    def add_video_to_plan(self, ad_id, local_file_path: str) -> str:
        \"\"\"往已有的在投全域计划追加一个素材（视频或图片，全量更新，自动保留旧素材）。\"\"\""""

new = """    def add_video_to_plan(self, ad_id, local_file_path: str):
        \"\"\"往已有的在投全域计划追加一个素材（视频或图片，全量更新，自动保留旧素材）。
        返回 (ad_id, material_id) 元组。\"\"\""""

if old in content:
    content = content.replace(old, new)
    print("✅ 修改方法定义成功")
else:
    print("❌ 没找到方法定义")

# 找到返回值，修改成返回元组
old_return = """            if resp_json.get("code") == 0:
                return str(resp_json.get("data", {}).get("ad_id", ad_id))"""

new_return = """            if resp_json.get("code") == 0:
                # 获取 material_id
                material_id = new_image_id if is_image else video_id
                return (str(resp_json.get("data", {}).get("ad_id", ad_id)), material_id)"""

if old_return in content:
    content = content.replace(old_return, new_return)
    print("✅ 修改返回值成功")
else:
    print("❌ 没找到返回值")

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

# 2. 修改 ad_routes.py 里的调用，接收 material_id
file_path2 = r"ad\routes\ad_routes.py"

with open(file_path2, "r", encoding="utf-8") as f:
    content2 = f.read()

old_call = """        try:
            new_ad_id = ad_service.add_video_to_plan(req.plan_id, f_path)
            result = AdLaunchResult(
                success=True,
                local_file_path=f_path,
                advertiser_id=advertiser_id,
                ad_plan_id=new_ad_id,
                audit_status="PENDING",
                error_msg=f"已把素材追加到所选投放计划{req.plan_id}下（未新建计划）",
            )"""

new_call = """        try:
            new_ad_id, new_material_id = ad_service.add_video_to_plan(req.plan_id, f_path)
            result = AdLaunchResult(
                success=True,
                local_file_path=f_path,
                advertiser_id=advertiser_id,
                ad_plan_id=new_ad_id,
                material_id=new_material_id,
                audit_status="PENDING",
                error_msg=f"已把素材追加到所选投放计划{req.plan_id}下（未新建计划）",
            )"""

if old_call in content2:
    content2 = content2.replace(old_call, new_call)
    print("✅ 修改调用成功")
else:
    print("❌ 没找到调用")

with open(file_path2, "w", encoding="utf-8") as f:
    f.write(content2)

print("完成")
