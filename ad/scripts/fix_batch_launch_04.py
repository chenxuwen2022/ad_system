with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 修复变量名错误
content = content.replace('const mat = allMaterials.find(m => m.name === name);', 
                          'const mat = ALL.find(m => m.name === name);')

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已修复变量名错误')
