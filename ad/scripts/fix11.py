file_path = r"static\wellflow-html\15-素材详情-新版.html"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 找到默认的假图片，替换成空的
old = """    <div class="player" id="playerBox">
      <img src="https://aka.doubaocdn.com/s/i5DqANPjnO" alt="素材预览">
      <span class="badge">01 / 03</span>
    </div>"""

new = """    <div class="player" id="playerBox">
      <div style="color:#999;padding:40px;">加载中...</div>
    </div>"""

if old in content:
    content = content.replace(old, new)
    print("✅ 替换默认假图片成功")
else:
    print("❌ 没找到默认假图片")

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

print("完成")
