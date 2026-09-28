with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 修复匹配逻辑
old_match = '''    const name = card.querySelector('.ttl')?.textContent || '';
    const type = card.querySelector('.tag-l')?.textContent || '图片';
    // 从渲染数据中获取完整信息
    const mat = ALL.find(m => m.name === name);'''

new_match = '''    const name = card.querySelector('.ttl')?.textContent || '';
    const type = card.querySelector('.tag-l')?.textContent || '图片';
    // 从渲染数据中获取完整信息（页面上显示的是去掉扩展名的名称）
    const mat = ALL.find(m => {
      const fn = m.name.replace(/\\.[^.]+$/, '');
      return fn === name;
    });'''

content = content.replace(old_match, new_match)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已修复素材匹配逻辑')
