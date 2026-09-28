import os

file_path = r"ad\douyin_api.py"

# 读取文件
with open(file_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

# 找到要修改的行
old_line = '            new_image_id = self.upload_local_media_get_material_id(tmp_file, "image")\n'
new_lines = '''            # 用原始文件名作为素材名，把临时文件重命名后再上传
            import shutil
            original_name = os.path.basename(local_file_path)
            renamed_tmp = os.path.join(os.path.dirname(tmp_file), "renamed_" + original_name)
            shutil.copy2(tmp_file, renamed_tmp)
            new_image_id = self.upload_local_media_get_material_id(renamed_tmp, "image")
            os.remove(renamed_tmp)
'''

# 替换
for i, line in enumerate(lines):
    if line == old_line:
        lines[i] = new_lines
        print(f"修改第 {i+1} 行")
        break

# 保存文件
with open(file_path, "w", encoding="utf-8") as f:
    f.writelines(lines)

print("✅ 修改完成")
