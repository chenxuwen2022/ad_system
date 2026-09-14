import asyncio
import sys
from pathlib import Path

# Windows 下 psycopg 异步连接要求 SelectorEventLoop。
# 两层保障：
# 1) 替换 asyncio 默认 policy（旧版 uvicorn 路径）；
# 2) uvicorn 0.36+ 的 asyncio_loop_factory 在 Windows 上硬编码返回 ProactorEventLoop（无视 policy），
#    必须同时替换 uvicorn 的 loop 工厂，CLI 与 uvicorn.run 才都能生效。
if sys.platform == "win32":
    try:
        asyncio.DefaultEventLoopPolicy = asyncio.WindowsSelectorEventLoopPolicy
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        import uvicorn.loops.asyncio as _uv_la
        import uvicorn.loops.auto as _uv_auto

        def _selector_loop_factory(use_subprocess: bool = False):
            return asyncio.SelectorEventLoop

        _uv_la.asyncio_loop_factory = _selector_loop_factory
        _uv_auto.asyncio_loop_factory = _selector_loop_factory
    except Exception:
        pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from routes.upload_routes import router as upload_router
from routes.ad_routes import router as ad_router
from config import MEDIA_STORAGE_PATH
from db import init_db
from token_manager import get_token_mgr

# ── 子系统二：电商商拍（WellFlow）──
from wellflow.app.api.tasks import router as wf_tasks_router
from wellflow.app.sse import router as wf_sse_router
from wellflow.app.config import settings as wf_settings
from wellflow.app.main import init_wellflow_runtime

app = FastAPI(title="AI 电商运营中台（广告投放 + 电商商拍）")

# CORS（商拍子系统前端跨域调用需要；对广告系统同源调用无影响）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# ── 子系统一：广告投放（原 ad_system 全部路由，路径不变）──
app.include_router(upload_router)
app.include_router(ad_router)

# ── 子系统二：电商商拍（WellFlow，API 路径与独立运行时一致）──
app.include_router(wf_tasks_router, prefix="/api")
app.include_router(wf_sse_router)
_wf_upload_dir = Path(wf_settings.upload_dir).resolve()
_wf_upload_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=_wf_upload_dir), name="wellflow_uploads")


@app.get("/health", tags=["系统"], summary="健康检查（含两个子系统状态）")
def health():
    from wellflow.app.main import get_checkpointer, get_graph
    return {
        "status": "ok",
        "ad_system": "ok",
        "wellflow": "ok",
        "langgraph_available": get_graph() is not None,
        "checkpointer_available": get_checkpointer() is not None,
    }


@app.on_event("startup")
async def startup_event():
    # 第一步：广告投放系统初始化
    init_db()
    print("[ok] 数据库表初始化完成")
    _ = get_token_mgr()
    print(f"广告投放系统启动，media目录: {MEDIA_STORAGE_PATH}")
    # 第二步：商拍子系统初始化（checkpointer + graph，PG 不可用时自动降级）
    await init_wellflow_runtime()
    print("✅ 商拍子系统（WellFlow）就绪")


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
