file_path = r'C:\Users\33082\PycharmProjects\wellflow-saas-backend\static\wellflow-html\04-投放台.html'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 在顶部工具栏加"同步状态"按钮
old_toolbar = '''<div class="bar-r">
    <button class="btn ghost" onclick="openLaunchModal()">↗ 上传到投放计划</button>
    <button class="btn ghost" onclick="openUploadModal()">本地上传</button>
</div>'''

new_toolbar = '''<div class="bar-r">
    <button class="btn ghost" onclick="syncMaterialStatus()">🔄 同步状态</button>
    <button class="btn ghost" onclick="openLaunchModal()">↗ 上传到投放计划</button>
    <button class="btn ghost" onclick="openUploadModal()">本地上传</button>
</div>'''

content = content.replace(old_toolbar, new_toolbar)

# 在 JavaScript 部分加同步函数
old_js_end = '''// 详情弹窗
function openDetail(url){'''

new_js_end = '''// 同步素材状态
function syncMaterialStatus(){
  if(!confirm('确定要同步素材状态吗？\\n\\n将从千川查询最新审核状态，更新本地数据库。')) return;
  
  const btn = event.target;
  const oldText = btn.textContent;
  btn.textContent = '同步中...';
  btn.disabled = true;
  
  fetch('/api/sync_material_status', { method: 'POST' })
    .then(r => r.json())
    .then(result => {
      if(result.success){
        alert('✅ ' + result.msg + '\\n\\n同步了 ' + result.total + ' 条素材，更新了 ' + result.updated + ' 条状态。');
        // 重新加载页面
        location.reload();
      } else {
        alert('❌ 同步失败：' + (result.error || '未知错误'));
        btn.textContent = oldText;
        btn.disabled = false;
      }
    })
    .catch(err => {
      alert('❌ 同步失败：' + err.message);
      btn.textContent = oldText;
      btn.disabled = false;
    });
}

// 详情弹窗
function openDetail(url){'''

content = content.replace(old_js_end, new_js_end)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已添加同步按钮和函数')
