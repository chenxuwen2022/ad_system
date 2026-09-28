import sys
sys.path.insert(0, '.')
from ad.douyin_api import DouYinAdService

# 配置
ADVERTISER_ID = "1788316608893963"
PLAN_ID = "1876997685331028"
SKU_ID = "3770434824473542984"

# 找一个测试素材（从 upload_materials 目录找一个图片）
import os
upload_dir = r"C:\Users\33082\PycharmProjects\ad_system\upload_materials"
test_file = None
for f in os.listdir(upload_dir):
    if f.endswith('.png') or f.endswith('.jpg'):
        test_file = os.path.join(upload_dir, f)
        break

if not test_file:
    print("❌ 没有找到测试素材")
    sys.exit(1)

print(f"测试素材: {test_file}")
print(f"广告主ID: {ADVERTISER_ID}")
print(f"计划ID: {PLAN_ID}")
print(f"SKU ID: {SKU_ID}")
print("=" * 80)

# 创建服务
ad_service = DouYinAdService(advertiser_id=ADVERTISER_ID)

# 调用投放接口
print("开始投放...")
try:
    # 使用 add_video_to_plan 方法（这个方法支持图片和视频）
    new_ad_id = ad_service.add_video_to_plan(PLAN_ID, test_file)
    
    print("=" * 80)
    print(f"✅ 投放成功！")
    print(f"新的创意ID: {new_ad_id}")
    print(f"素材文件: {test_file}")
    print(f"计划ID: {PLAN_ID}")
    
except Exception as e:
    print("=" * 80)
    print(f"❌ 投放失败: {e}")
    import traceback
    traceback.print_exc()
