file_path = r'C:\Users\33082\PycharmProjects\wellflow-saas-backend\ad\routes\ad_routes.py'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 在 material_stats 接口后面加同步接口
sync_api = '''

# ============ 同步素材状态 ============
@router.post("/api/sync_material_status")
async def sync_material_status():
    """同步素材投放状态：从千川查询最新审核状态，更新本地数据库"""
    db = SessionLocal()
    try:
        # 查询所有有 material_id 的投放记录
        rows = db.query(MaterialLaunchDB).filter(
            MaterialLaunchDB.material_id != "",
            MaterialLaunchDB.mode == "real"
        ).all()
        
        if not rows:
            return {"success": True, "msg": "没有需要同步的素材", "updated": 0}
        
        updated = 0
        errors = []
        
        # 按 advertiser_id 分组，避免重复创建服务
        ad_services = {}
        
        for row in rows:
            try:
                # 获取或创建广告服务
                aid = row.advertiser_id
                if aid not in ad_services:
                    ad_services[aid] = DouYinAdService(advertiser_id=aid)
                ad_service = ad_services[aid]
                
                # 查询素材库信息
                material_id = row.material_id
                mtype = "视频" if row.detail and "video" in row.detail.lower() else "图片"
                
                # 调用千川接口查询素材状态
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
                ).json()
                
                if rj.get("code") == 0:
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
                            updated += 1
                else:
                    errors.append(f"素材 {material_id} 查询失败: {rj.get('message', '')}")
                    
            except Exception as e:
                errors.append(f"素材 {row.material_id} 同步异常: {str(e)}")
        
        db.commit()
        
        return {
            "success": True,
            "msg": f"同步完成，更新了 {updated} 条记录",
            "updated": updated,
            "total": len(rows),
            "errors": errors[:10]  # 只返回前10个错误
        }
        
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        db.close()
'''

# 在 material_stats 函数后面插入
content = content.replace(
    '@router.get("/api/material_launch_status")',
    sync_api + '\n@router.get("/api/material_launch_status")'
)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已添加同步接口')
