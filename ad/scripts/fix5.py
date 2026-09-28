file_path = r"static\wellflow-html\04-投放台.html"

with open(file_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

# 找到 start 变量的定义，改成 startIdx
for i in range(len(lines)):
    if "var start = (curPage - 1) * pageSize;" in lines[i]:
        lines[i] = lines[i].replace("var start = ", "var startIdx = ")
        print(f"Line {i+1}: 修改 start -> startIdx")
    if "var pageData = filtered.slice(start, start + pageSize);" in lines[i]:
        lines[i] = lines[i].replace("slice(start, start + pageSize)", "slice(startIdx, startIdx + pageSize)")
        print(f"Line {i+1}: 修改 slice")

with open(file_path, "w", encoding="utf-8") as f:
    f.writelines(lines)

print("完成")
