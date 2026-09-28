with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 给搜索框添加 id 和事件
old_search = '<div class="search">🔍 <input placeholder="搜索素材名称、ID..."></div>'
new_search = '<div class="search">🔍 <input id="searchInput" placeholder="搜索素材名称、ID..." oninput="onSearch()"></div>'
content = content.replace(old_search, new_search)

# 2. 添加搜索变量
old_var = "let filterStatus = '全部';"
new_var = "let filterStatus = '全部';\nlet searchKeyword = '';"
content = content.replace(old_var, new_var)

# 3. 在 render 函数中添加搜索筛选逻辑
# 找到筛选部分
old_filter = '''  // 筛选
  var filtered = ALL;
  if(filterType !== '全部'){
    filtered = filtered.filter(m => (m.type||'image') === (filterType==='图片'?'image':'video'));
  }'''

new_filter = '''  // 筛选
  var filtered = ALL;
  if(filterType !== '全部'){
    filtered = filtered.filter(m => (m.type||'image') === (filterType==='图片'?'image':'video'));
  }
  // 搜索筛选
  if(searchKeyword){
    var kw = searchKeyword.toLowerCase();
    filtered = filtered.filter(m => 
      (m.name||'').toLowerCase().includes(kw) || 
      String(m.id||'').includes(kw)
    );
  }'''

content = content.replace(old_filter, new_filter)

# 4. 添加 onSearch 函数
old_func = 'function filterByStatCard'
new_func = '''function onSearch(){
  searchKeyword = document.getElementById('searchInput').value.trim();
  curPage = 1;
  render();
}
function filterByStatCard'''

content = content.replace(old_func, new_func)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 搜索功能实现完成')
