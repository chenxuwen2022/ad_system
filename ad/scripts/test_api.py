import sys
sys.path.insert(0, '.')
import requests

# 后端接口地址
url = "http://127.0.0.1:8000/api/ad/launch"

# 测试参数
data = {
    "platform": "douyin",
    "advertiser_id": "1788316608893963",
    "local_file_path": r"C:\Users\33082\PycharmProjects\ad_system\upload_materials\260919-绒绒外套-童装-纺织C组-婷婷1.png",
    "plan_id": "1876997685331028",
    "plan_name": "【商城】童装-绒绒套装260922",
    "product_ids": ["3770434824473542984"],
    "tags": [],
    "test_mode": False
}

print("测试接口:", url)
print("参数:", data)
print("=" * 80)

try:
    response = requests.post(url, json=data, timeout=60)
    print("状态码:", response.status_code)
    print("响应:", response.text[:500])
    
except Exception as e:
    print("❌ 调用失败:", e)
