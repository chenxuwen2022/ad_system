with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 公共标签按钮
old_public = '''<button class="fopt">浏览标签分组</button><button class="fopt active">全部</button>
      <button class="fopt">卖点</button><button class="fopt">内容类型</button><button class="fopt">场景</button><button class="fopt">营销节点</button><button class="fopt">清除标签条件</button>'''

new_public = '''<button class="fopt" onclick="alert('打开标签分组')">浏览标签分组</button><button class="fopt active" onclick="filterByTag(this,'公共标签','全部')">全部</button>
      <button class="fopt" onclick="filterByTag(this,'公共标签','卖点')">卖点</button><button class="fopt" onclick="filterByTag(this,'公共标签','内容类型')">内容类型</button><button class="fopt" onclick="filterByTag(this,'公共标签','场景')">场景</button><button class="fopt" onclick="filterByTag(this,'公共标签','营销节点')">营销节点</button><button class="fopt" onclick="clearAllTags()">清除标签条件</button>'''

content = content.replace(old_public, new_public)

# 2. 具体标签 chip
old_chips = '''<button class="chip on">舒适透气</button><button class="chip on">显瘦版型</button><button class="chip on">质感面料</button><button class="chip on">百城通勤</button>
        <button class="chip">商品展示</button><button class="chip">穿搭推荐</button><button class="chip">细节特写</button>
        <button class="chip warn">日常通勤</button><button class="chip warn">周末出游</button><button class="chip warn">国家生活</button>
        <button class="chip warn">秋季上新</button><button class="chip warn">双11预热</button>'''

new_chips = '''<button class="chip on" onclick="toggleChip(this)">舒适透气</button><button class="chip on" onclick="toggleChip(this)">显瘦版型</button><button class="chip on" onclick="toggleChip(this)">质感面料</button><button class="chip on" onclick="toggleChip(this)">百城通勤</button>
        <button class="chip" onclick="toggleChip(this)">商品展示</button><button class="chip" onclick="toggleChip(this)">穿搭推荐</button><button class="chip" onclick="toggleChip(this)">细节特写</button>
        <button class="chip warn" onclick="toggleChip(this)">日常通勤</button><button class="chip warn" onclick="toggleChip(this)">周末出游</button><button class="chip warn" onclick="toggleChip(this)">国家生活</button>
        <button class="chip warn" onclick="toggleChip(this)">秋季上新</button><button class="chip warn" onclick="toggleChip(this)">双11预热</button>'''

content = content.replace(old_chips, new_chips)

# 3. 个人标签
old_personal = '''<button class="fopt active">全部</button><button class="fopt">无个人标签</button><button class="fopt">有个人标签</button>
      <button class="fopt">重点关注</button><button class="fopt">待复盘</button><button class="fopt">重置</button><button class="fopt" style="color:var(--green)">✎ 管理标签</button>'''

new_personal = '''<button class="fopt active" onclick="filterByTag(this,'个人标签','全部')">全部</button><button class="fopt" onclick="filterByTag(this,'个人标签','无个人标签')">无个人标签</button><button class="fopt" onclick="filterByTag(this,'个人标签','有个人标签')">有个人标签</button>
      <button class="fopt" onclick="filterByTag(this,'个人标签','重点关注')">重点关注</button><button class="fopt" onclick="filterByTag(this,'个人标签','待复盘')">待复盘</button><button class="fopt" onclick="resetAllFilters()">重置</button><button class="fopt" style="color:var(--green)" onclick="alert('打开标签管理')">✎ 管理标签</button>'''

content = content.replace(old_personal, new_personal)

# 4. 高级搜索下拉框
old_selects = '''<select class="drop"><option>全部渠道</option></select><select class="drop"><option>全部店铺</option></select>
      <select class="drop"><option>全部SPU</option></select><select class="drop"><option>全部SKC</option></select>
      <select class="drop"><option>全部SKU</option></select>'''

new_selects = '''<select class="drop" onchange="filterBySelect(this,'渠道')"><option>全部渠道</option></select><select class="drop" onchange="filterBySelect(this,'店铺')"><option>全部店铺</option></select>
      <select class="drop" onchange="filterBySelect(this,'SPU')"><option>全部SPU</option></select><select class="drop" onchange="filterBySelect(this,'SKC')"><option>全部SKC</option></select>
      <select class="drop" onchange="filterBySelect(this,'SKU')"><option>全部SKU</option></select>'''

content = content.replace(old_selects, new_selects)

# 5. 排序、消耗、系统标签下拉框 + 重置按钮
old_bottom = '''<select class="drop" style="max-width:140px"><option>排序 最新上传</option></select>
      <select class="drop" style="max-width:140px"><option>消耗 不限</option></select>
      <select class="drop" style="max-width:140px"><option>系统标签 全部</option></select>
      <button class="fbtn">↻ 重置</button>'''

new_bottom = '''<select class="drop" style="max-width:140px" onchange="filterBySelect(this,'排序')"><option>排序 最新上传</option></select>
      <select class="drop" style="max-width:140px" onchange="filterBySelect(this,'消耗')"><option>消耗 不限</option></select>
      <select class="drop" style="max-width:140px" onchange="filterBySelect(this,'系统标签')"><option>系统标签 全部</option></select>
      <button class="fbtn" onclick="resetAllFilters()">↻ 重置</button>'''

content = content.replace(old_bottom, new_bottom)

# 6. 添加新的JS函数
js_functions = '''
function toggleChip(el){
  el.classList.toggle('on');
  curPage = 1;
  render();
}
function clearAllTags(){
  document.querySelectorAll('.chip').forEach(c=>c.classList.remove('on'));
  curPage = 1;
  render();
}
function filterBySelect(el, category){
  var value = el.options[el.selectedIndex].text;
  if(!window.filters) window.filters = {};
  window.filters[category] = value;
  curPage = 1;
  render();
}
function resetAllFilters(){
  window.filters = {};
  filterStatus = '全部';
  filterType = '全部';
  // 重置所有按钮状态
  document.querySelectorAll('.fopt').forEach((el,i)=>{
    el.classList.remove('active');
  });
  document.querySelectorAll('.frow:first-child .fopt:first-child').forEach(el=>el.classList.add('active'));
  curPage = 1;
  render();
}
'''

# 在 filterByTag 函数前添加
content = content.replace('function filterByTag(el, category, value){', js_functions + '\nfunction filterByTag(el, category, value){')

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 修改完成')
