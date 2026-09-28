with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/ad/douyin_api.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 修改 _get_json 函数，让它返回错误信息而不是 None
old_get_json = '''        def _get_json(url, params, retries=2):
            """千川接口偶发限流/网络抖动：失败时重试，避免整个汇总返回 0。"""
            for attempt in range(retries):
                try:
                    j = requests.get(url, headers=self.headers, params=params, timeout=30).json()
                    if j.get("code") == 0:
                        return j
                except Exception:
                    pass
                if attempt < retries - 1:
                    _time.sleep(0.5)
            return None'''

new_get_json = '''        def _get_json(url, params, retries=2):
            """千川接口偶发限流/网络抖动：失败时重试，避免整个汇总返回 0。"""
            for attempt in range(retries):
                try:
                    j = requests.get(url, headers=self.headers, params=params, timeout=30).json()
                    if j.get("code") == 0:
                        return j
                    else:
                        print(f"[plans] API error: code={j.get('code')}, msg={j.get('message')}")
                except Exception as e:
                    print(f"[plans] Request exception: {e}")
                if attempt < retries - 1:
                    _time.sleep(0.5)
            return None'''

content = content.replace(old_get_json, new_get_json)

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/ad/douyin_api.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已修改，添加错误日志')
