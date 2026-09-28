with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 修改筛选逻辑，直接用素材本身的 biz_status 字段
old_filter = '''  if(filterStatus !== '全部'){
    filtered = filtered.filter(m => {
      var info = infoOf(m);
      var st = mapBiz(info.biz_status);
      return st === filterStatus;
    });
  }'''

new_filter = '''  if(filterStatus !== '全部'){
    filtered = filtered.filter(m => {
      // 优先用素材本身的 biz_status
      var st = mapBiz(m.biz_status);
      // 如果素材本身没有状态，再从 STATUS 中查找
      if(st === '未提交' && !m.biz_status){
        var info = infoOf(m);
        st = mapBiz(info.biz_status);
      }
      return st === filterStatus;
    });
  }'''

content = content.replace(old_filter, new_filter)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已修复状态筛选逻辑')
