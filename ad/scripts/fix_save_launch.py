import re

file_path = r'C:\Users\33082\PycharmProjects\wellflow-saas-backend\ad\routes\ad_routes.py'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 修改 _save_launch_record 函数，加上 material_id 参数
old_func = '''def _save_launch_record(file_path: str, status: str, mode: str,
                        plan_id: str = "", plan_name: str = "",
                        product_id: str = "", detail: str = "",
                        advertiser_id: str = "", budget: float = 0,
                        biz_status: str = ""):
    """记录一次素材投放历史，用于素材库展示投放状态。
    biz_status：业务状态（待审核/审核驳回/通过-待投放/直播间已投放/商城已投放/已投放商品+直播间/放弃测试）。
    SQLite material_launch（现有功能）+ PostgreSQL launch_record（投放记录表）双写。"""
    if not file_path:
        return
    db = SessionLocal()
    try:
        db.add(MaterialLaunchDB(
            file_path=file_path, status=status, mode=mode,
            biz_status=biz_status or "",
            plan_id=plan_id or "", plan_name=plan_name or "",
            product_id=product_id or "", detail=(detail or "")[:300],
        ))
        db.commit()
    finally:
        db.close()'''

new_func = '''def _save_launch_record(file_path: str, status: str, mode: str,
                        plan_id: str = "", plan_name: str = "",
                        product_id: str = "", detail: str = "",
                        advertiser_id: str = "", budget: float = 0,
                        biz_status: str = "", material_id: str = ""):
    """记录一次素材投放历史，用于素材库展示投放状态。
    biz_status：业务状态（待审核/审核驳回/通过-待投放/直播间已投放/商城已投放/已投放商品+直播间/放弃测试）。
    SQLite material_launch（现有功能）+ PostgreSQL launch_record（投放记录表）双写。"""
    if not file_path:
        return
    db = SessionLocal()
    try:
        db.add(MaterialLaunchDB(
            file_path=file_path, status=status, mode=mode,
            biz_status=biz_status or "",
            plan_id=plan_id or "", plan_name=plan_name or "",
            product_id=product_id or "", detail=(detail or "")[:300],
            material_id=material_id or "", advertiser_id=advertiser_id or "",
        ))
        db.commit()
    finally:
        db.close()'''

content = content.replace(old_func, new_func)

# 找到真实投放成功后的地方，把 material_id 传进去
# 先找到调用 _save_launch_record 的地方
# 真实投放的地方应该在测试模式之后

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已修改 _save_launch_record 函数')
