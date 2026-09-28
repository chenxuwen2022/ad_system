file_path = r'C:\Users\33082\PycharmProjects\wellflow-saas-backend\ad\routes\ad_routes.py'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 修改同步接口，加上驳回原因
old_sync = '''                if rj.get("code") == 0:
                    lst = rj.get("data", {}).get("list", []) or []
                    if lst:
                        item = lst[0]
                        audit_status = item.get("audit_status", "")
                        
                        # 映射千川审核状态到业务状态
                        new_biz_status = ""
                        if audit_status == "AUDIT_STATUS_APPROVED":
                            new_biz_status = "通过-待投放"
                        elif audit_status == "AUDIT_STATUS_REJECTED":
                            new_biz_status = "审核驳回"
                        elif audit_status == "AUDIT_STATUS_PENDING":
                            new_biz_status = "待审核"
                        else:
                            new_biz_status = row.biz_status  # 保持原状态
                        
                        # 如果状态变了，更新数据库
                        if new_biz_status and new_biz_status != row.biz_status:
                            row.biz_status = new_biz_status
                            row.detail = f"同步更新：{audit_status}"
                            updated += 1'''

new_sync = '''                if rj.get("code") == 0:
                    lst = rj.get("data", {}).get("list", []) or []
                    if lst:
                        item = lst[0]
                        audit_status = item.get("audit_status", "")
                        reject_reason = item.get("reject_reason", "") or item.get("audit_reason", "") or ""
                        
                        # 映射千川审核状态到业务状态
                        new_biz_status = ""
                        if audit_status == "AUDIT_STATUS_APPROVED":
                            new_biz_status = "通过-待投放"
                        elif audit_status == "AUDIT_STATUS_REJECTED":
                            new_biz_status = "审核驳回"
                        elif audit_status == "AUDIT_STATUS_PENDING":
                            new_biz_status = "待审核"
                        else:
                            new_biz_status = row.biz_status  # 保持原状态
                        
                        # 更新数据库（状态或驳回原因变化了就更新）
                        changed = False
                        if new_biz_status and new_biz_status != row.biz_status:
                            row.biz_status = new_biz_status
                            changed = True
                        if reject_reason and reject_reason != row.reject_reason:
                            row.reject_reason = reject_reason
                            changed = True
                        if changed:
                            row.detail = f"同步更新：{audit_status}" + (f"，原因：{reject_reason}" if reject_reason else "")
                            updated += 1'''

content = content.replace(old_sync, new_sync)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已修改同步接口，加上驳回原因')
