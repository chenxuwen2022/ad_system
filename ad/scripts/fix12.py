file_path = r"static\wellflow-html\15-素材详情-新版.html"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 把 initMaterial() 调用放到 DOMContentLoaded 事件里
old = """initMaterial();

// 从接口获取素材详情"""

new = """// 等 DOM 加载完成后再执行
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initMaterial);
} else {
  initMaterial();
}

// 从接口获取素材详情"""

if old in content:
    content = content.replace(old, new)
    print("✅ 修改成功")
else:
    print("❌ 没找到要修改的内容")

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

print("完成")
