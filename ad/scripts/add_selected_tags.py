with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 在筛选区域后面添加已选条件标签区域
old_filter_end = '''    <div class="frow">
      <div class="fl">个人标签</div>
      <div class="fopts" id="personalTagList">
        <button class="fopt active" onclick="filterByTag(this,'个人标签','全部')">全部</button>
      </div>
    </div>
  </div>'''

new_filter_end = '''    <div class="frow">
      <div class="fl">个人标签</div>
      <div class="fopts" id="personalTagList">
        <button class="fopt active" onclick="filterByTag(this,'个人标签','全部')">全部</button>
      </div>
    </div>
    <!-- 已选条件标签 -->
    <div class="frow" id="selectedTagsRow" style="display:none">
      <div class="fl">已选条件</div>
      <div class="fopts" id="selectedTagsList"></div>
    </div>
  </div>'''

content = content.replace(old_filter_end, new_filter_end)

# 2. 添加已选条件标签样式
old_style = '.fopt{...}'
new_style = '''.selected-tag{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;border-radius:20px;background:rgba(204,255,0,0.15);color:var(--green);font-size:13px;cursor:pointer;margin-right:8px}
.selected-tag .cat{opacity:0.7}
.selected-tag .close{font-size:14px;line-height:1;opacity:0.7}
.selected-tag .close:hover{opacity:1;color:var(--red)}
.clear-all{color:var(--text-3);font-size:13px;cursor:pointer;text-decoration:underline;margin-left:8px}
.clear-all:hover{color:var(--red)}
.fopt'''

content = content.replace(old_style, new_style)

# 3. 添加更新已选条件标签的函数
old_reset = 'function resetAllFilters(){'
new_reset = '''function updateSelectedTags(){
  var row = document.getElementById('selectedTagsRow');
  var list = document.getElementById('selectedTagsList');
  var tags = [];
  
  // 收集所有筛选条件
  if(window.filters){
    for(var cat in window.filters){
      var val = window.filters[cat];
      if(val && val !== '全部'){
        tags.push({category: cat, value: val});
      }
    }
  }
  if(filterType && filterType !== '全部'){
    tags.push({category: '素材类型', value: filterType});
  }
  if(filterStatus && filterStatus !== '全部'){
    tags.push({category: '计划阶段', value: filterStatus});
  }
  if(searchKeyword){
    tags.push({category: '搜索', value: searchKeyword});
  }
  
  if(tags.length === 0){
    row.style.display = 'none';
    list.innerHTML = '';
    return;
  }
  
  row.style.display = 'flex';
  var html = tags.map((t,i)=>
    '<span class="selected-tag">' +
    '<span class="cat">'+t.category+'：</span>' +
    '<span class="val">'+t.value+'</span>' +
    '<span class="close" onclick="removeFilter('+i+')">×</span>' +
    '</span>'
  ).join('');
  html += '<span class="clear-all" onclick="resetAllFilters()">清空全部</span>';
  list.innerHTML = html;
}

function removeFilter(index){
  var tags = [];
  if(window.filters){
    for(var cat in window.filters){
      var val = window.filters[cat];
      if(val && val !== '全部'){
        tags.push({category: cat, value: val});
      }
    }
  }
  if(filterType && filterType !== '全部'){
    tags.push({category: '素材类型', value: filterType});
  }
  if(filterStatus && filterStatus !== '全部'){
    tags.push({category: '计划阶段', value: filterStatus});
  }
  if(searchKeyword){
    tags.push({category: '搜索', value: searchKeyword});
  }
  
  if(index < tags.length){
    var t = tags[index];
    if(t.category === '素材类型'){
      filterType = '全部';
    } else if(t.category === '计划阶段'){
      filterStatus = '全部';
    } else if(t.category === '搜索'){
      searchKeyword = '';
      document.getElementById('searchInput').value = '';
    } else {
      delete window.filters[t.category];
    }
  }
  curPage = 1;
  render();
}

function resetAllFilters(){'''

content = content.replace(old_reset, new_reset)

# 4. 在 render 函数末尾调用 updateSelectedTags
old_render_end = "  g.innerHTML=pageData.map(m=>{"
new_render_end = """  updateSelectedTags();
  g.innerHTML=pageData.map(m=>{"""

content = content.replace(old_render_end, new_render_end)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已选条件标签功能完成')
