file_path = r'C:\Users\33082\PycharmProjects\wellflow-saas-backend\static\wellflow-html\04-投放台.html'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 在正确的位置加同步按钮
old_tools = '''    <div class="tools">
      <button class="tbtn" onclick="openSetting('09-店铺设置.html')">店铺设置</button>
      <button class="tbtn" onclick="openSetting('10-素材筛选配置.html')">素材筛选配置</button>
      <button class="tbtn" onclick="openSetting('11-投放任务.html')">投放任务</button>
      <button class="tbtn primary" onclick="location.href='12-本地上传.html'">↑ 本地上传</button>
    </div>'''

new_tools = '''    <div class="tools">
      <button class="tbtn" onclick="syncMaterialStatus()">🔄 同步状态</button>
      <button class="tbtn" onclick="openSetting('09-店铺设置.html')">店铺设置</button>
      <button class="tbtn" onclick="openSetting('10-素材筛选配置.html')">素材筛选配置</button>
      <button class="tbtn" onclick="openSetting('11-投放任务.html')">投放任务</button>
      <button class="tbtn primary" onclick="location.href='12-本地上传.html'">↑ 本地上传</button>
    </div>'''

content = content.replace(old_tools, new_tools)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已在正确位置添加同步按钮')
