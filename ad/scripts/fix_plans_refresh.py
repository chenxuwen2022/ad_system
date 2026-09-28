with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/15-选择计划.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 添加 refresh 参数，强制刷新
content = content.replace(
    "fetch('/api/plans?advertiser_id='+encodeURIComponent(aid))",
    "fetch('/api/plans?advertiser_id='+encodeURIComponent(aid)+'&refresh=true')"
)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/15-选择计划.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已添加 refresh 参数')
