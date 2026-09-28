with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/17-投放结果.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 修改结果显示，支持批量投放
old_result = '''<script>
// 从 sessionStorage 读投放结果
const chosen=JSON.parse(sessionStorage.getItem('launch_chosen')||'[]');
document.getElementById('resultBody').innerHTML=chosen.map(c=>
  '<div class="plan">'+
    '<div class="nm">'+(c.plan_name||'')+'</div>'+
    '<div class="pid">计划 ID: '+c.plan_id+'</div>'+
    (c.product_ids&&c.product_ids[0]?'<div class="sku">SKU ID: '+c.product_ids[0]+'</div>':'')+
  '</div>'+
  '<span class="ok">已关联</span>'+
  '<span class="ok">已启动投放</span>'
).join('');
</script>'''

new_result = '''<script>
// 从 sessionStorage 读投放结果
const result = JSON.parse(sessionStorage.getItem('launchResult') || '{}');
const chosen = JSON.parse(sessionStorage.getItem('launch_chosen') || '[]');

const body = document.getElementById('resultBody');

if(result.total){
  // 批量投放结果
  let html = '<div style="margin-bottom:16px;padding:12px;background:rgba(204,255,0,0.1);border-radius:8px">';
  html += '<div style="font-size:16px;font-weight:600;margin-bottom:8px">投放结果汇总</div>';
  html += '<div style="display:flex;gap:24px;font-size:14px">';
  html += '<span>总任务：<b>'+result.total+'</b></span>';
  html += '<span style="color:#52C41A">成功：<b>'+result.success+'</b></span>';
  html += '<span style="color:#EA6668">失败：<b>'+result.failed+'</b></span>';
  html += '</div></div>';
  
  html += '<div style="font-size:14px;font-weight:600;margin:16px 0 8px">详细结果</div>';
  html += result.details.map((r,i)=>
    '<div style="display:flex;align-items:center;padding:12px;border:1px solid #333;border-radius:8px;margin-bottom:8px">'+
      '<div style="flex:1">'+
        '<div style="font-weight:500">'+r.material+'</div>'+
        '<div style="color:#888;font-size:12px;margin-top:4px">投放计划：'+r.plan+'</div>'+
        (r.error?'<div style="color:#EA6668;font-size:12px;margin-top:4px">错误：'+r.error+'</div>':'')+
      '</div>'+
      '<span class="'+(r.success?'ok':'err')+'">'+(r.success?'投放成功':'投放失败')+'</span>'+
    '</div>'
  ).join('');
  
  body.innerHTML = html;
}else{
  // 单次投放结果
  body.innerHTML = chosen.map(c=>
    '<div class="plan">'+
      '<div class="nm">'+(c.plan_name||'')+'</div>'+
      '<div class="pid">计划 ID: '+c.plan_id+'</div>'+
      (c.product_ids&&c.product_ids[0]?'<div class="sku">SKU ID: '+c.product_ids[0]+'</div>':'')+
    '</div>'+
    '<span class="ok">已关联</span>'+
    '<span class="ok">已启动投放</span>'
  ).join('');
}
</script>'''

content = content.replace(old_result, new_result)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/17-投放结果.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已修改 17-投放结果.html，支持批量投放结果显示')
