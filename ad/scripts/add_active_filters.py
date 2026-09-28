with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 在筛选区域下方添加已选条件标签区域
old_filter_end = '''<div class="grid" id="grid"></div>'''

new_filter_end = '''<!-- 已选筛选条件 -->
<div id="activeFilters" style="display:none;padding:10px 14px;margin-bottom:14px;background:var(--card2);border-radius:10px;border:1px solid var(--line)">
  <div style="font-size:13px;color:var(--text-3);margin-bottom:8px">已选筛选条件：</div>
  <div id="activeFilterChips" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"></div>
</div>

<div class="grid" id="grid"></div>'''

content = content.replace(old_filter_end, new_filter_end)

# 2. 修改 render 函数，添加已选条件标签渲染
old_render_start = '''function render(){
  const g=document.getElementById('grid');'''

new_render_start = '''function render(){
  const g=document.getElementById('grid');
  renderActiveFilters();'''

content = content.replace(old_render_start, new_render_start)

# 3. 添加 renderActiveFilters 函数
old_func_end = "function mapBiz(biz){"

new_func_end = '''function renderActiveFilters(){
  const container = document.getElementById('activeFilterChips');
  const box = document.getElementById('activeFilters');
  const filters = [];
  
  // 收集所有选中的筛选条件
  if(filterType && filterType !== '全部'){
    filters.push({key:'type', label:'素材类型', value:filterType});
  }
  if(window.filters){
    for(const [cat, val] of Object.entries(window.filters)){
      if(val && val !== '全部'){
        filters.push({key:cat, label:cat, value:val});
      }
    }
  }
  if(searchKeyword){
    filters.push({key:'search', label:'搜索', value:searchKeyword});
  }
  if(startDateFilter){
    filters.push({key:'startDate', label:'开始日期', value:startDateFilter});
  }
  if(endDateFilter){
    filters.push({key:'endDate', label:'结束日期', value:endDateFilter});
  }
  
  if(filters.length === 0){
    box.style.display = 'none';
    return;
  }
  
  box.style.display = 'block';
  let html = '';
  filters.forEach((f, i) => {
    html += `<span style="display:inline-flex;align-items:center;gap:6px;padding:4px 10px;background:rgba(204,255,0,0.1);border:1px solid var(--green);border-radius:14px;font-size:12px;color:var(--text)">
      ${f.label}: ${f.value}
      <span style="cursor:pointer;color:var(--red);margin-left:2px" onclick="removeFilter('${f.key}')">✕</span>
    </span>`;
  });
  html += '<span style="cursor:pointer;color:var(--green);font-size:12px;margin-left:8px" onclick="resetAllFilters()">清除全部</span>';
  container.innerHTML = html;
}

function removeFilter(key){
  if(key === 'type'){
    filterType = '全部';
    // 重置素材类型按钮
    document.querySelectorAll('.frow')[0].querySelectorAll('.fopt').forEach(x=>x.classList.remove('active'));
    document.querySelectorAll('.frow')[0].querySelector('.fopt').classList.add('active');
  } else if(key === 'search'){
    searchKeyword = '';
    document.getElementById('searchInput').value = '';
  } else if(key === 'startDate'){
    startDateFilter = '';
    delete window.filters['开始时间'];
  } else if(key === 'endDate'){
    endDateFilter = '';
    delete window.filters['结束时间'];
  } else {
    delete window.filters[key];
  }
  curPage = 1;
  render();
}

function mapBiz(biz){"""

content = content.replace(old_func_end, new_func_end)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已选筛选条件标签功能完成')
