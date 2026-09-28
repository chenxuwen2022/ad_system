with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 替换 view-toggle 区域
old_view = '''    <div class="view-toggle">
      <button class="fbtn">作者/搜索</button>
      <button class="fbtn">上传时间 yyyy/mm/日</button>
      <button class="fbtn">至 yyyy/mm/日</button>
      <button class="fbtn">▦ 网格</button>
      <button class="fbtn">☰ 列表</button>
    </div>'''

new_view = '''    <div class="view-toggle">
      <select class="fbtn" style="width:auto" onchange="filterBySelect(this,'作者')">
        <option>作者/搜索</option>
      </select>
      <input type="date" class="fbtn" style="width:auto" onchange="filterBySelect(this,'开始时间')" placeholder="开始日期">
      <span style="color:var(--text-3);font-size:13px">至</span>
      <input type="date" class="fbtn" style="width:auto" onchange="filterBySelect(this,'结束时间')" placeholder="结束日期">
      <button class="fbtn primary" id="gridBtn" onclick="toggleView('grid')">▦ 网格</button>
      <button class="fbtn" id="listBtn" onclick="toggleView('list')">☰ 列表</button>
    </div>'''

content = content.replace(old_view, new_view)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 修复完成')
