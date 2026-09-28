file_path = r"static\wellflow-html\04-投放台.html"

with open(file_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

# 找到 start 变量的位置，改成 startIdx
for i in range(len(lines)):
    if "const start = (curPage - 1) * pageSize;" in lines[i]:
        lines[i] = lines[i].replace("const start = (curPage - 1) * pageSize;", "const startIdx = (curPage - 1) * pageSize;")
        print(f"Line {i+1}: 修改 start -> startIdx")
    if "const pageData = filtered.slice(start, start + pageSize);" in lines[i]:
        lines[i] = lines[i].replace("const pageData = filtered.slice(start, start + pageSize);", "const pageData = filtered.slice(startIdx, startIdx + pageSize);")
        print(f"Line {i+1}: 修改 pageData slice")

# 找到 g.innerHTML=pageData.map 那行，改成带索引的
for i in range(len(lines)):
    if "g.innerHTML=pageData.map(m=>{" in lines[i]:
        lines[i] = lines[i].replace("g.innerHTML=pageData.map(m=>{", "g.innerHTML=pageData.map((m, idx)=>{")
        print(f"Line {i+1}: 修改 map 加索引")
    
    # 找到 detailUrl 那行，删除
    if "const detailUrl = " in lines[i] and "encodeURIComponent(m.name)" in lines[i]:
        lines[i] = ""
        print(f"Line {i+1}: 删除 detailUrl 变量")
    
    # 找到 return 那行，改成用索引
    if 'onclick="openDetail(this.dataset.url)"' in lines[i]:
        lines[i] = lines[i].replace(
            '<div class="m" style="cursor:pointer" onclick="openDetail(this.dataset.url)" data-url="\'',
            '<div class="m" style="cursor:pointer" onclick="openDetailByIndex(\'+(startIdx+idx)+\')\">'
        )
        # 再处理一下引号
        lines[i] = lines[i].replace(
            "data-url=\"\"",
            ""
        )
        print(f"Line {i+1}: 修改 onclick 用索引")

with open(file_path, "w", encoding="utf-8") as f:
    f.writelines(lines)

print("完成")
