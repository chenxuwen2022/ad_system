from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from routes.upload_routes import router as upload_router
from routes.ad_routes import router as ad_router
from config import MEDIA_STORAGE_PATH
from db import init_db
from token_manager import get_token_mgr

app = FastAPI(title="商城广告投放系统")
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(upload_router)
app.include_router(ad_router)


@app.on_event("startup")
async def startup_event():
    # 第一步：先创建数据库表
    init_db()
    print("[ok] 数据库表初始化完成")
    # 第二步：再初始化 token 管理器
    _ = get_token_mgr()
    print(f"广告投放系统启动，media目录: {MEDIA_STORAGE_PATH}")
    # 首次授权需要通过单独的管理命令或受保护的后台操作兑换 auth_code
    # auth_code 一次性且短时有效，不能在每次启动时重复兑换


def show_advertisers():
    """打印当前token已授权的广告主ID列表"""
    try:
        tm = get_token_mgr()
        accounts = tm.get_advertiser_ids()
    except Exception as e:
        print(f"查询失败:{e}")
        return
    if not accounts:
        print("当前token未授权任何广告主，请先通过 /api/token/fetch 完成授权")
        return
    print(f"已授权的广告主ID（共 {len(accounts)} 个）:")
    for acc in accounts:
        print(f"  {acc.get('advertiser_id')}  {acc.get('advertiser_name')}  ({acc.get('account_role')})")


def show_qianchuan():
    """打印千川投放账户下的可投放抖音号与可投商品列表（需千川权限授权后可用）"""
    import requests
    from config import DOUYIN_CONFIG
    tm = get_token_mgr()
    advertiser_id = DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
    headers = {"Access-Token": tm.get_access_token()}
    base = "https://api.oceanengine.com/open_api/v1.0/qianchuan"
    print(f"千川投放账户（默认）: {advertiser_id}")

    # 1. 可投放抖音号
    try:
        r = requests.get(f"{base}/aweme/authorized/get/",
                         headers=headers,
                         params={"advertiser_id": advertiser_id}, timeout=30)
        j = r.json()
        if j.get("code") == 0:
            awemes = j.get("data", {}).get("aweme_id_list", [])
            print(f"\n可投放抖音号（{len(awemes)} 个，填到 config.py 的 AWEME_ID）:")
            for a in awemes:
                print(f"  {a.get('aweme_id')}  {a.get('aweme_name')}  ({a.get('bind_type')})")
        else:
            print(f"\n查询可投放抖音号失败 code:{j.get('code')}, msg:{j.get('message')}（若为40002请先重新授权千川权限）")
    except Exception as e:
        print(f"\n查询可投放抖音号异常:{e}")

    # 2. 可投商品列表
    try:
        r = requests.get(f"{base}/product/available/get/",
                         headers=headers,
                         params={"advertiser_id": advertiser_id, "page": 1, "page_size": 50}, timeout=30)
        j = r.json()
        if j.get("code") == 0:
            products = j.get("data", {}).get("product_list", [])
            total = j.get("data", {}).get("page_info", {}).get("total_number", len(products))
            print(f"\n可投商品（共 {total} 个，填到 config.py 的 PRODUCT_IDS）:")
            for p in products[:50]:
                print(f"  {p.get('id')}  {p.get('name')}")
            if len(products) > 50:
                print(f"  ...（仅显示前50个）")
        else:
            print(f"\n查询可投商品失败 code:{j.get('code')}, msg:{j.get('message')}（若为40002请先重新授权千川权限）")
    except Exception as e:
        print(f"\n查询可投商品异常:{e}")


if __name__ == "__main__":
    import sys
    import uvicorn

    if "--show-advertisers" in sys.argv:
        show_advertisers()
        sys.exit(0)

    if "--show-qianchuan" in sys.argv:
        show_qianchuan()
        sys.exit(0)

    # 不要reload，windows下reload会多进程导致sqlite问题
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
