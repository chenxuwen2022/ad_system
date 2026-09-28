with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 删除重复的 filterByStatus 函数（第360行的旧版本）
old_func1 = '''function filterByStatus(status, el){
  filterStatus = status === '全部' ? '全部' : status;
  document.querySelectorAll('.stat').forEach(s=>s.classList.remove('accent'));
  el.classList.add('accent');
  page = 1;
  render();
}
function render(){'''

new_func1 = '''function render(){'''

content = content.replace(old_func1, new_func1)

# 2. 修复变量名不一致（page -> curPage）
content = content.replace('  page = 1;', '  curPage = 1;')

# 3. 确保 viewMode 变量已定义
if 'let viewMode' not in content:
    content = content.replace("let searchKeyword = '';", "let searchKeyword = '';\nlet viewMode = 'grid';")

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 修复完成')
