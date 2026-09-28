with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 给视图切换按钮添加 id
content = content.replace('<button class="fbtn" onclick="toggleView(\'grid\')">▦ 网格</button>', '<button class="fbtn" id="gridBtn" onclick="toggleView(\'grid\')">▦ 网格</button>')
content = content.replace('<button class="fbtn" onclick="toggleView(\'list\')">☰ 列表</button>', '<button class="fbtn" id="listBtn" onclick="toggleView(\'list\')">☰ 列表</button>')

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 修复完成')
