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

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from ad.routes.upload_routes import router as upload_router
from ad.routes.ad_routes import router as ad_router
from wellflow.app.api.outfit import router as wf_outfit_router
from wellflow.app.api.uploads import router as wf_uploads_router
from ad.config import MEDIA_STORAGE_PATH
from ad.db import init_db
from ad.token_manager import get_token_mgr

# ── 子系统二：电商商拍（WellFlow）──
from wellflow.app.api.tasks import router as wf_tasks_router
from wellflow.app.api.products import router as wf_products_router
from wellflow.app.api.mannequins import router as wf_mannequins_router
from wellflow.app.api.chat import router as wf_chat_router
from wellflow.app.api.conversations import router as wf_conversations_router
from wellflow.app.sse import router as wf_sse_router
from wellflow.app.config import settings as wf_settings
from wellflow.app.runtime import (
    init_wellflow_runtime,
    get_checkpointer as wf_get_checkpointer,
    get_graph as wf_get_graph,
    shutdown_wellflow_runtime,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动：先初始化广告投放系统，再初始化商拍子系统（PG 不可用时自动降级）。"""
    # 第一步：广告投放系统初始化
    init_db()
    print("[ok] 数据库表初始化完成")
    _ = get_token_mgr()
    print(f"广告投放系统启动，media目录: {MEDIA_STORAGE_PATH}")
    # 第二步：商拍子系统初始化（checkpointer + graph，PG 不可用时自动降级）
    await init_wellflow_runtime()
    print("✅ 商拍子系统（WellFlow）就绪")
    # 第三步：后台预热千川数据（素材报表 + 商品→素材映射），避免用户首次点击等待 40-80s
    try:
        import threading as _th
        def _prewarm():
            try:
                from ad.douyin_api import DouYinAdService, DOUYIN_CONFIG
                svc = DouYinAdService(advertiser_id=str(DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")))
                svc.get_all_materials_report()          # 素材报表（千川 90 天，约 40s）
                svc.get_products_material_map()          # 商品→素材映射（约 20s）
                print("[预热] 千川素材报表与商品映射已就绪")
            except Exception as e:
                print(f"[预热] 后台预热失败（不影响使用，用户首次点击时会自动拉取）: {e}")
        _th.Thread(target=_prewarm, daemon=True).start()
    except Exception:
        pass
    yield
    await shutdown_wellflow_runtime()


app = FastAPI(title="AI 电商运营中台（广告投放 + 电商商拍）", lifespan=lifespan)

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
app.include_router(wf_outfit_router, prefix="/api")    # 穿搭库(数据库版,wellflow/app/api/outfit.py)
app.include_router(wf_uploads_router, prefix="/api")  # WellFlow 统一图片上传

# ── 子系统二：电商商拍（WellFlow，API 路径与独立运行时一致）──
app.include_router(wf_tasks_router, prefix="/api")
app.include_router(wf_products_router, prefix="/api")
app.include_router(wf_mannequins_router, prefix="/api")

# WellFlow 通用图片上传（不绑定 task，供 SKU/模特/任意素材库先传后提）
from wellflow.app.api.uploads import router as wf_uploads_router
app.include_router(wf_uploads_router, prefix="/api")

# WellFlow 对话入口（意图路由 —— 把自然语言映射到 LangGraph 节点）
app.include_router(wf_chat_router, prefix="/api")

# WellFlow 会话管理（Conversation-centric 历史列表 + 详情 + 删除）
app.include_router(wf_conversations_router, prefix="/api")

app.include_router(wf_sse_router)
_wf_upload_dir = Path(wf_settings.upload_dir).resolve()
_wf_upload_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=_wf_upload_dir), name="wellflow_uploads")


@app.get("/health", tags=["系统"], summary="健康检查（含两个子系统状态）")
def health():
    return {
        "status": "ok",
        "ad_system": "ok",
        "wellflow": "ok",
        "langgraph_available": wf_get_graph() is not None,
        "checkpointer_available": wf_get_checkpointer() is not None,
    }


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
