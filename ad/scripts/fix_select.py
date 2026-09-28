with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 修复 filterBySelect 函数，处理不同类型的元素
old_func = '''function filterBySelect(el, category){
  var value = el.options[el.selectedIndex].text;
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
  if(!window.filters) window.filters = {};
  window.filters[category] = value;
  curPage = 1;
  render();
}'''

content = content.replace(old_func, new_func)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 修复完成')
