import sys
sys.path.insert(0, '.')
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

# 查询素材列表
r = client.get('/api/db_materials')
data = r.json()
print('素材数量：', len(data.get('data', [])))
if data.get('data'):
    print('第一个素材：')
    import json
    print(json.dumps(data['data'][0], indent=2, ensure_ascii=False))
