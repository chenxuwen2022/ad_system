import sys
sys.path.insert(0, ".")

import requests

# 调用 /api/launch_records 接口
r = requests.get("http://127.0.0.1:8000/api/launch_records?limit=100")
data = r.json()

print(f"接口返回 {len(data.get('data', []))} 条记录")
print()

# 按 advertiser_id 分组统计
from collections import Counter
advertiser_counts = Counter(d.get('advertiser_id') for d in data.get('data', []))

print("按 advertiser_id 分组:")
for aid, count in advertiser_counts.items():
    print(f"  {aid}: {count} 条")
