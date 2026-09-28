with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 修改 filterBySelect 函数，日期选择不立即查询
old_func = '''function filterBySelect(el, category){
  var value = '';
  if(el.tagName === 'SELECT'){
    value = el.options[el.selectedIndex].text;
  } else if(el.tagName === 'INPUT'){
    value = el.value;
  }
  if(category === '作者'){
    authorFilter = value;
  } else if(category === '开始时间'){
    startDateFilter = value;
  } else if(category === '结束时间'){
    endDateFilter = value;
  }
  if(!window.filters) window.filters = {};
  window.filters[category] = value;
  curPage = 1;
  render();
}'''

new_func = '''function filterBySelect(el, category){
  var value = '';
  if(el.tagName === 'SELECT'){
    value = el.options[el.selectedIndex].text;
  } else if(el.tagName === 'INPUT'){
    value = el.value;
  }
  if(category === '作者'){
    authorFilter = value;
    if(!window.filters) window.filters = {};
    window.filters[category] = value;
    curPage = 1;
    render();
  } else if(category === '开始时间'){
    startDateFilter = value;
    // 选择开始日期不立即查询，等结束日期选完再查
  } else if(category === '结束时间'){
    endDateFilter = value;
    if(!window.filters) window.filters = {};
    window.filters[category] = value;
    curPage = 1;
    render();
  }
}'''

content = content.replace(old_func, new_func)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 修改完成')
