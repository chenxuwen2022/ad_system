with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 在搜索框后面添加作者、时间筛选、视图切换
old_search_area = '''<div class="lhead">
      <div class="lt">全部素材 <span class="cnt">0</span><span class="total">共 0 份素材</span></div>
      <div class="search">🔍 <input id="searchInput" placeholder="搜索素材名称、ID..." oninput="onSearch()"></div>
    </div>'''

new_search_area = '''<div class="lhead">
      <div class="lt">全部素材 <span class="cnt">0</span><span class="total">共 0 份素材</span></div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <div class="search">🔍 <input id="searchInput" placeholder="搜索素材名称、ID..." oninput="onSearch()"></div>
        <select class="drop" style="width:auto" onchange="filterBySelect(this,'作者')">
          <option>作者/搜索</option>
        </select>
        <input type="date" class="drop" style="width:auto" onchange="filterBySelect(this,'开始时间')" placeholder="开始日期">
        <span style="color:var(--text-3);font-size:13px">至</span>
        <input type="date" class="drop" style="width:auto" onchange="filterBySelect(this,'结束时间')" placeholder="结束日期">
        <button class="fbtn" onclick="toggleView('grid')" id="gridBtn">▦ 网格</button>
        <button class="fbtn" onclick="toggleView('list')" id="listBtn">☰ 列表</button>
      </div>
    </div>'''

content = content.replace(old_search_area, new_search_area)

# 2. 添加视图切换函数和变量
old_var = "let searchKeyword = '';"
new_var = "let searchKeyword = '';\nlet viewMode = 'grid';"
content = content.replace(old_var, new_var)

# 3. 添加 toggleView 函数
old_func = 'function onSearch()'
new_func = '''function toggleView(mode){
  viewMode = mode;
  document.getElementById('gridBtn').classList.toggle('primary', mode==='grid');
  document.getElementById('listBtn').classList.toggle('primary', mode==='list');
  render();
}
function onSearch()'''

content = content.replace(old_func, new_func)

# 4. 修改 render 函数中的 grid 部分，支持列表视图
old_render_grid = "  g.innerHTML = '<div style=\"grid-column:1/-1;padding:60px;text-align:center;color:var(--text-3)\">暂无上传素材，点右上\"本地上传\"添加</div>';"

new_render_grid = """  if(viewMode === 'list'){
    g.className = 'list-view';
  } else {
    g.className = 'grid';
  }
  if(!ALL.length){
    g.innerHTML = '<div style="grid-column:1/-1;padding:60px;text-align:center;color:var(--text-3)">暂无上传素材，点右上\"本地上传\"添加</div>';
  }"""

content = content.replace(old_render_grid, new_render_grid)

# 5. 添加列表视图样式
old_style = '.grid{display:grid;grid-template-columns:repeat(6,1fr);gap:14px}'
new_style = '''.grid{display:grid;grid-template-columns:repeat(6,1fr);gap:14px}
.list-view{display:flex;flex-direction:column;gap:10px}
.list-view .m{display:flex;align-items:center;padding:10px;gap:14px}
.list-view .m .cover{width:80px;height:100px;flex-shrink:0}
.list-view .m .info{flex:1}
.list-view .m .data{display:flex;gap:16px;padding:0;background:none}'''

content = content.replace(old_style, new_style)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 筛选和视图切换功能完成')
