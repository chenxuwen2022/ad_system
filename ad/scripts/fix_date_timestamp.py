with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 修复日期筛选逻辑 - mtime是秒级时间戳，需要乘以1000
old_date_filter = '''  // 日期筛选
  if(startDateFilter){
    filtered = filtered.filter(m => {
      var d = new Date(m.mtime || m.create_time || 0);
      return d >= new Date(startDateFilter);
    });
  }
  if(endDateFilter){
    filtered = filtered.filter(m => {
      var d = new Date(m.mtime || m.create_time || 0);
      return d <= new Date(endDateFilter + ' 23:59:59');
    });
  }'''

new_date_filter = '''  // 日期筛选（mtime是秒级时间戳）
  if(startDateFilter){
    filtered = filtered.filter(m => {
      var ts = (m.mtime || m.create_time || 0) * 1000;
      var d = new Date(ts);
      return d >= new Date(startDateFilter);
    });
  }
  if(endDateFilter){
    filtered = filtered.filter(m => {
      var ts = (m.mtime || m.create_time || 0) * 1000;
      var d = new Date(ts);
      return d <= new Date(endDateFilter + ' 23:59:59');
    });
  }'''

content = content.replace(old_date_filter, new_date_filter)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 修复完成')
