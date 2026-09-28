file_path = r'C:\Users\33082\PycharmProjects\wellflow-saas-backend\static\wellflow-html\04-投放台.html'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 修复同步结果显示
old_alert = '''      if(result.success){
        alert('✅ ' + result.msg + '\\n\\n同步了 ' + result.total + ' 条素材，更新了 ' + result.updated + ' 条状态。');'''

new_alert = '''      if(result.success){
        const total = result.total || 0;
        const updated = result.updated || 0;
        alert('✅ ' + (result.msg || '同步完成') + '\\n\\n同步了 ' + total + ' 条素材，更新了 ' + updated + ' 条状态。');'''

content = content.replace(old_alert, new_alert)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已修复同步结果显示')
