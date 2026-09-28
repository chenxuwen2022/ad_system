import re

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 修复 filterByTag 中的双引号问题
content = content.replace("'投放场景\"\"'", "'投放场景'")
content = content.replace("'主类目\"\"'", "'主类目'")
content = content.replace("'一级分类\"\"'", "'一级分类'")
content = content.replace("'二级分类\"\"'", "'二级分类'")

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 修复完成')
