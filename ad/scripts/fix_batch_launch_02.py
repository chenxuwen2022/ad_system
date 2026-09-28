with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/16-确认投放.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 修改 doLaunch 函数，支持批量投放
old_launch = '''function doLaunch(){
  if(chosen.length===0){alert('请先选择计划');return;}
  
  // 收集投放参数
  const params = new URLSearchParams(location.search);
  const aid = params.get('aid') || '';
  const shop = params.get('shop') || '';
  
  // 获取选中的素材和计划
  const selectedMaterials = JSON.parse(sessionStorage.getItem('selectedMaterials') || '[]');
  const selectedPlans = chosen; // 已选中的计划
  
  if(selectedMaterials.length === 0){
    alert('没有选中的素材');
    return;
  }
  
  // 调用后端投放接口
  fetch('/api/ad/launch', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      platform: 'douyin',
      advertiser_id: aid,
      local_file_path: selectedMaterials[0].path || selectedMaterials[0],
      plan_id: selectedPlans[0].ad_id || selectedPlans[0],
      plan_name: selectedPlans[0].name || '',
      product_ids: selectedPlans[0].products || [],
      tags: [],
      test_mode: false  // 真实投放
    })
  })
  .then(r=>r.json())
  .then(result=>{
    // 保存结果到 sessionStorage
    sessionStorage.setItem('launchResult', JSON.stringify({
      success: result.success,
      plan: selectedPlans[0].name || '',
      material: selectedMaterials[0].name || selectedMaterials[0],
      error: result.error_msg || ''
    }));
    location.href='17-投放结果.html';
  })
  .catch(err=>{
    alert('投放失败：' + err.message);
  });
}'''

new_launch = '''function doLaunch(){
  if(chosen.length===0){alert('请先选择计划');return;}
  
  // 收集投放参数
  const params = new URLSearchParams(location.search);
  const aid = params.get('aid') || '';
  const shop = params.get('shop') || '';
  
  // 获取选中的素材和计划
  const selectedMaterials = JSON.parse(sessionStorage.getItem('selectedMaterials') || '[]');
  const selectedPlans = chosen; // 已选中的计划
  
  if(selectedMaterials.length === 0){
    alert('没有选中的素材');
    return;
  }
  
  // 批量投放：每个素材投到每个计划
  const results = [];
  let completed = 0;
  const total = selectedMaterials.length * selectedPlans.length;
  
  // 显示进度
  const btn = document.querySelector('.btn.primary');
  btn.textContent = '正在投放... 0/' + total;
  btn.disabled = true;
  
  selectedMaterials.forEach(material => {
    selectedPlans.forEach(plan => {
      fetch('/api/ad/launch', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          platform: 'douyin',
          advertiser_id: aid,
          local_file_path: material.path || material,
          plan_id: plan.plan_id,
          plan_name: plan.plan_name || '',
          product_ids: plan.product_ids || [],
          tags: [],
          test_mode: false  // 真实投放
        })
      })
      .then(r=>r.json())
      .then(result=>{
        results.push({
          material: material.name || material,
          plan: plan.plan_name || plan.plan_id,
          success: result.success,
          error: result.error_msg || ''
        });
        completed++;
        btn.textContent = '正在投放... ' + completed + '/' + total;
        
        if(completed === total){
          // 全部完成，保存结果并跳转
          sessionStorage.setItem('launchResult', JSON.stringify({
            total: total,
            success: results.filter(r=>r.success).length,
            failed: results.filter(r=>!r.success).length,
            details: results
          }));
          location.href='17-投放结果.html';
        }
      })
      .catch(err=>{
        results.push({
          material: material.name || material,
          plan: plan.plan_name || plan.plan_id,
          success: false,
          error: err.message
        });
        completed++;
        btn.textContent = '正在投放... ' + completed + '/' + total;
        
        if(completed === total){
          sessionStorage.setItem('launchResult', JSON.stringify({
            total: total,
            success: results.filter(r=>r.success).length,
            failed: results.filter(r=>!r.success).length,
            details: results
          }));
          location.href='17-投放结果.html';
        }
      });
    });
  });
}'''

content = content.replace(old_launch, new_launch)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/16-确认投放.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已修改 16-确认投放.html，支持批量投放')
