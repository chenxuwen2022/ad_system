file_path = r'C:\Users\33082\PycharmProjects\wellflow-saas-backend\ad\routes\ad_routes.py'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 修改真实投放成功后的 _save_launch_record 调用，加上 material_id
old_call = '''        if result.success:
            _save_material_marks(req.local_file_path, req.tags)
            _save_launch_record(req.local_file_path, "success", "real",
                                plan_id=req.plan_id, plan_name=req.plan_name or req.plan_id or "", product_id=",".join(req.product_ids or []),
                                detail="已追加到投放计划",
                                advertiser_id=advertiser_id or "", budget=req.budget or 0,
                                biz_status="待审核")'''

new_call = '''        if result.success:
            _save_material_marks(req.local_file_path, req.tags)
            _save_launch_record(req.local_file_path, "success", "real",
                                plan_id=req.plan_id, plan_name=req.plan_name or req.plan_id or "", product_id=",".join(req.product_ids or []),
                                detail="已追加到投放计划",
                                advertiser_id=advertiser_id or "", budget=req.budget or 0,
                                biz_status="待审核",
                                material_id=getattr(result, 'material_id', '') or '')'''

content = content.replace(old_call, new_call)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已修改真实投放调用，传入 material_id')
