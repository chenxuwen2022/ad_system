file_path = r"ad\routes\ad_routes.py"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 注释掉删除本地文件的代码
old = "            delete_media_file(req.local_file_path)"
new = "            # delete_media_file(req.local_file_path)  # 保留本地文件"

if old in content:
    content = content.replace(old, new)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    print("✅ 修改成功")
else:
    print("❌ 没找到要修改的内容")
