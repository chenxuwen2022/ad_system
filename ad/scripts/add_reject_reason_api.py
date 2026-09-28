file_path = r'C:\Users\33082\PycharmProjects\wellflow-saas-backend\ad\routes\ad_routes.py'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 修改 db_materials 接口，加上 reject_reason 字段
old_data = '''            data.append({
                "id": r.id,
                "name": fname,
                "path": real_path,
                "type": ftype,
                "size": os.path.getsize(real_path),
                "mtime": os.path.getmtime(real_path),
                "biz_status": r.biz_status,
                "plan_name": r.plan_name,
                "detail": r.detail,
            })'''

new_data = '''            data.append({
                "id": r.id,
                "name": fname,
                "path": real_path,
                "type": ftype,
                "size": os.path.getsize(real_path),
                "mtime": os.path.getmtime(real_path),
                "biz_status": r.biz_status,
                "plan_name": r.plan_name,
                "detail": r.detail,
                "reject_reason": getattr(r, 'reject_reason', '') or '',
            })'''

content = content.replace(old_data, new_data)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已在 db_materials 接口中加上 reject_reason 字段')
