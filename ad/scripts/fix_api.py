with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 修正接口路径
content = content.replace('/api/material_categories', '/api/categories')
content = content.replace('/api/material_tags?tag_type=public', '/api/tags?tag_type=public')
content = content.replace('/api/material_tags?tag_type=personal', '/api/tags?tag_type=personal')

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 接口路径修正完成')
