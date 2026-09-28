with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 在高级搜索区域后面添加已选条件标签
old_advanced = '''    <div class="frow"><div class="fl"></div><div class="fopts">
      <select class="drop" style="max-width:140px" onchange="filterBySelect(this,'排序')"><option>排序 最新上传</option></select>
      <select class="drop" style="max-width:140px" onchange="filterBySelect(this,'消耗')"><option>消耗 不限</option></select>
      <select class="drop" style="max-width:140px" onchange="filterBySelect(this,'系统标签')"><option>系统标签 全部</option></select>
      <button class="fbtn" onclick="resetAllFilters()">↻ 重置</button>'''

new_advanced = '''    <!-- 已选条件标签 -->
    <div class="frow" id="selectedTagsRow" style="display:none">
      <div class="fl">已选条件</div>
      <div class="fopts" id="selectedTagsList"></div>
    </div>
    <div class="frow"><div class="fl"></div><div class="fopts">
      <select class="drop" style="max-width:140px" onchange="filterBySelect(this,'排序')"><option>排序 最新上传</option></select>
      <select class="drop" style="max-width:140px" onchange="filterBySelect(this,'消耗')"><option>消耗 不限</option></select>
      <select class="drop" style="max-width:140px" onchange="filterBySelect(this,'系统标签')"><option>系统标签 全部</option></select>
      <button class="fbtn" onclick="resetAllFilters()">↻ 重置</button>'''

content = content.replace(old_advanced, new_advanced)

# 2. 修复 updateSelectedTags 函数，添加空值判断
old_func = '''function updateSelectedTags(){
  var row = document.getElementById('selectedTagsRow');
  var list = document.getElementById('selectedTagsList');
  var tags = [];'''

new_func = '''function updateSelectedTags(){
  var row = document.getElementById('selectedTagsRow');
  var list = document.getElementById('selectedTagsList');
  if(!row || !list) return;
  var tags = [];'''

content = content.replace(old_func, new_func)

# 3. 添加样式
old_css = '.fopt{padding:6px 14px;border-radius:18px;background:var(--card2);border:1px solid var(--line);font-size:13px;cursor:pointer;white-space:nowrap}'
new_css = '''.selected-tag{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;border-radius:20px;background:rgba(204,255,0,0.15);color:var(--green);font-size:13px;cursor:pointer;margin-right:8px}
.selected-tag .cat{opacity:0.7}
.selected-tag .close{font-size:14px;line-height:1;opacity:0.7;cursor:pointer}
.selected-tag .close:hover{opacity:1;color:var(--red)}
.clear-all{color:var(--text-3);font-size:13px;cursor:pointer;text-decoration:underline;margin-left:8px}
.clear-all:hover{color:var(--red)}
.fopt{padding:6px 14px;border-radius:18px;background:var(--card2);border:1px solid var(--line);font-size:13px;cursor:pointer;white-space:nowrap}'''

content = content.replace(old_css, new_css)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 修复完成')
