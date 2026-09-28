with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 添加变量
old_var = "let viewMode = 'grid';"
new_var = """let viewMode = 'grid';
let authorFilter = '全部';
let startDateFilter = '';
let endDateFilter = '';"""
content = content.replace(old_var, new_var)

# 2. 修改 filterBySelect 函数
old_func = '''function filterBySelect(el, category){
  var value = '';
  if(el.tagName === 'SELECT'){
    value = el.options[el.selectedIndex].text;
  } else if(el.tagName === 'INPUT'){
    value = el.value;
  }
  if(!window.filters) window.filters = {};
  window.filters[category] = value;
  curPage = 1;
  render();
}'''

new_func = '''function filterBySelect(el, category){
  var value = '';
  if(el.tagName === 'SELECT'){
    value = el.options[el.selectedIndex].text;
  } else if(el.tagName === 'INPUT'){
    value = el.value;
  }
  if(category === '作者'){
    authorFilter = value;
  } else if(category === '开始时间'){
    startDateFilter = value;
  } else if(category === '结束时间'){
    endDateFilter = value;
  }
  if(!window.filters) window.filters = {};
  window.filters[category] = value;
  curPage = 1;
  render();
}'''

content = content.replace(old_func, new_func)

# 3. 修改 render 函数，添加日期筛选和视图切换
old_render_filter = '''  // 搜索筛选
  if(searchKeyword){
    var kw = searchKeyword.toLowerCase();
    filtered = filtered.filter(m => 
      (m.name||'').toLowerCase().includes(kw) || 
      String(m.id||'').includes(kw)
    );
  }'''

new_render_filter = '''  // 搜索筛选
  if(searchKeyword){
    var kw = searchKeyword.toLowerCase();
    filtered = filtered.filter(m => 
      (m.name||'').toLowerCase().includes(kw) || 
      String(m.id||'').includes(kw)
    );
  }
  // 日期筛选
  if(startDateFilter){
    filtered = filtered.filter(m => {
      var d = new Date(m.mtime || m.create_time || 0);
      return d >= new Date(startDateFilter);
    });
  }
  if(endDateFilter){
    filtered = filtered.filter(m => {
      var d = new Date(m.mtime || m.create_time || 0);
      return d <= new Date(endDateFilter + ' 23:59:59');
    });
  }'''

content = content.replace(old_render_filter, new_render_filter)

# 4. 修改 render 函数中的 grid 渲染，支持列表视图
old_render_grid = "  g.innerHTML=pageData.map(m=>{"

new_render_grid = """  // 设置视图模式
  if(viewMode === 'list'){
    g.className = 'list-view';
  } else {
    g.className = 'grid';
  }
  g.innerHTML=pageData.map(m=>{"""

content = content.replace(old_render_grid, new_render_grid)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 功能实现完成')
