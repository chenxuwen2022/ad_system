file_path = r"static\wellflow-html\04-投放台.html"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

old = """function openDetailByIndex(idx){
  const m = ALL[idx];
  if(!m) return;
  openDetail(detailUrl);
}"""

new = """function openDetailByIndex(idx){
  const m = ALL[idx];
  if(!m) return;
  const detailUrl = '15-素材详情-新版.html?name=' + encodeURIComponent(m.name) + '&type=' + encodeURIComponent(m.type || '') + '&path=' + encodeURIComponent(m.path || '');
  openDetail(detailUrl);
}"""

if old in content:
    content = content.replace(old, new)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    print("✅ 修复成功")
else:
    print("❌ 没找到要修改的内容")
