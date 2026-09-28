with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 修改 openLaunchModal 函数，保存选中的素材列表
old_open = '''function openLaunchModal(){
  if(getSelCount()===0) return;
  var f=document.getElementById('launchFrame');
  f.src='14-上传到投放计划.html?count='+getSelCount();
  document.getElementById('launchModal').style.display='flex';
}'''

new_open = '''function openLaunchModal(){
  if(getSelCount()===0) return;
  
  // 获取选中的素材列表
  const selectedMaterials = [];
  document.querySelectorAll('.m.sel').forEach(function(card){
    const name = card.querySelector('.ttl')?.textContent || '';
    const type = card.querySelector('.tag-l')?.textContent || '图片';
    // 从渲染数据中获取完整信息
    const mat = allMaterials.find(m => m.name === name);
    if(mat){
      selectedMaterials.push({
        name: mat.name,
        type: mat.type || 'image',
        path: mat.path || mat.file_path || '',
        id: mat.id || ''
      });
    }
  });
  
  // 保存到 sessionStorage
  sessionStorage.setItem('selectedMaterials', JSON.stringify(selectedMaterials));
  
  var f=document.getElementById('launchFrame');
  f.src='14-上传到投放计划.html?count='+getSelCount();
  document.getElementById('launchModal').style.display='flex';
}'''

content = content.replace(old_open, new_open)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已修改 04-投放台.html，保存选中素材列表')
