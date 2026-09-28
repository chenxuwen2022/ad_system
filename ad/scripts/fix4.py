file_path = r"static\wellflow-html\04-投放台.html"

with open(file_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

# 找到第 454 行（索引 453）
for i in range(len(lines)):
    if "onclick=\"openDetailByIndex(" in lines[i]:
        lines[i] = "    return '<div class=\"m\" style=\"cursor:pointer\" onclick=\"openDetailByIndex('+(startIdx+idx)+')\">'+\n"
        print(f"Line {i+1}: 修复拼接")
        break

with open(file_path, "w", encoding="utf-8") as f:
    f.writelines(lines)

print("完成")
