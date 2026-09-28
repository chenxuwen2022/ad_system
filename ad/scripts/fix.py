import re

file_path = r"static\wellflow-html\04-投放台.html"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 找到 goto 那行，替换成更安全的方式
old = """    const goto='15-素材详情-新版.html?name='+encodeURIComponent(m.name)+'&type='+encodeURIComponent(m.type||'')+'&path='+encodeURIComponent(m.path||'');
    return '<div class="m" style="cursor:pointer" onclick="openDetail(\\''+goto+'\\')">'"""

new = """    const detailUrl = '15-素材详情-新版.html?name=' + encodeURIComponent(m.name) + '&type=' + encodeURIComponent(m.type || '') + '&path=' + encodeURIComponent(m.path || '');
    return '<div class="m" style="cursor:pointer" onclick="openDetail(this.dataset.url)" data-url="' + detailUrl + '">'"""

if old in content:
    content = content.replace(old, new)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    print("✅ 修改成功")
else:
    print("❌ 没找到要修改的内容")
    # 打印一下附近的内容
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if "goto" in line:
            print(f"Line {i+1}: {line}")
