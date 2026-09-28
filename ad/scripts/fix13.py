file_path = r"static\wellflow-html\15-素材详情-新版.html"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 修复嵌套的 script 标签
old = """<script>
<script>
// 从 URL 获取索引"""

new = """<script>
// 从 URL 获取索引"""

if old in content:
    content = content.replace(old, new)
    print("✅ 修复 script 标签成功")
else:
    print("❌ 没找到嵌套的 script 标签")

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

print("完成")
