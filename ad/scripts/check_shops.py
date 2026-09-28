import sys
sys.path.insert(0, ".")

import requests

# 调用 /api/shops 接口
r = requests.get("http://127.0.0.1:8000/api/shops")
shops = r.json()

print("店铺列表:")
for s in shops:
    print(f"  {s.get('shop_name')}: {s.get('advertiser_id')}")
