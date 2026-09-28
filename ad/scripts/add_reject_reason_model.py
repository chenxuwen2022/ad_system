file_path = r'C:\Users\33082\PycharmProjects\wellflow-saas-backend\ad\db.py'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 在 material_id 后面加 reject_reason 字段
old_fields = '''    material_id = Column(String, nullable=False, default="")     # 千川素材ID，用于同步状态
    advertiser_id = Column(String, nullable=False, default="")   # 广告主ID
    create_time = Column(DateTime, default=datetime.datetime.now)'''

new_fields = '''    material_id = Column(String, nullable=False, default="")     # 千川素材ID，用于同步状态
    advertiser_id = Column(String, nullable=False, default="")   # 广告主ID
    reject_reason = Column(String, nullable=False, default="")   # 审核驳回原因
    create_time = Column(DateTime, default=datetime.datetime.now)'''

content = content.replace(old_fields, new_fields)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已在模型中添加 reject_reason 字段')
