file_path = r"static\wellflow-html\04-投放台.html"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 修改 onclick，直接传素材名称
old = """    return '<div class="m" style="cursor:pointer" onclick="openDetailByIndex('+(startIdx+idx)+')">'+"""

new = """    const safeName = m.name.replace(/'/g, "\\'");
    return '<div class="m" style="cursor:pointer" onclick="openDetailByName(\\''+safeName+'\\')">'+"""

if old in content:
    content = content.replace(old, new)
    print("✅ 修改 onclick 成功")
else:
    print("❌ 没找到 onclick 的旧内容")

# 2. 修改 openDetailByIndex 函数，改成 openDetailByName
old_func = """function openDetailByIndex(idx){
  const m = ALL[idx];
  if(!m) return;
  const detailUrl = '15-素材详情-新版.html?name=' + encodeURIComponent(m.name) + '&type=' + encodeURIComponent(m.type || '') + '&path=' + encodeURIComponent(m.path || '');
  openDetail(detailUrl);
}"""

new_func = """function openDetailByName(name){
  const m = ALL.find(x => x.name === name);
  if(!m) return;
  const detailUrl = '15-素材详情-新版.html?name=' + encodeURIComponent(m.name) + '&type=' + encodeURIComponent(m.type || '') + '&path=' + encodeURIComponent(m.path || '');
  openDetail(detailUrl);
}"""

if old_func in content:
    content = content.replace(old_func, new_func)
    print("✅ 修改函数成功")
else:
    print("❌ 没找到函数的旧内容")

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

print("完成")
