"""WellFlow 后端 - FastAPI 应用入口"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from wellflow.app.api.tasks import router as tasks_router
from wellflow.app.api.products import router as products_router
from wellflow.app.api.mannequins import router as mannequins_router
from wellflow.app.api.uploads import router as uploads_router
from wellflow.app.api.chat import router as chat_router
from wellflow.app.api.conversations import router as conversations_router
from wellflow.app.api.utils import ok, fail, StandardResponse
from wellflow.app.sse import router as sse_router
from wellflow.app.config import settings


# ---------------------------------------------------------------------------
# 全局单例：checkpointer + graph（在 lifespan / 宿主启动时初始化）
# ---------------------------------------------------------------------------

_checkpointer: Any = None
_checkpointer_cm: Any = None  # 保留连接池 context manager 引用，防止 GC 关闭连接
_graph: Any = None


def get_checkpointer():
    return _checkpointer


def get_graph():
    return _graph


# ---------------------------------------------------------------------------
# 初始化：AsyncPostgresSaver + LangGraph 父图（可被独立 lifespan 或宿主应用调用）
# ---------------------------------------------------------------------------


async def init_wellflow_runtime() -> None:
    """初始化 checkpointer 与 graph。

    供两种场景使用：
    1. 本应用独立运行（uvicorn wellflow.app.main:app）→ lifespan 调用；
    2. 作为子系统并入宿主应用（ad_system/main.py）→ 宿主 startup 调用。

    初始化失败时降级（_checkpointer/_graph = None），不影响其余 API。
    """
    global _checkpointer, _checkpointer_cm, _graph

    _checkpointer = None
    _graph = None
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        conn_string = settings.database_url_async.replace(
            "postgresql+asyncpg://", "postgresql://"
        )
        # PG 不可用时快速降级，避免启动卡在连接超时
        if "?" in conn_string:
            conn_string += "&connect_timeout=5"
        else:
            conn_string += "?connect_timeout=5"
        cm = AsyncPostgresSaver.from_conn_string(conn_string)
        saver = await cm.__aenter__()  # 进入连接池生命周期（应用运行期间保持）
        await saver.setup()
        _checkpointer = saver
        _checkpointer_cm = cm  # 保留 cm 引用防止 GC 回收连接池
        print("✅ [商拍子系统] AsyncPostgresSaver 初始化完成，checkpoint 表已就绪")
    except Exception as exc:
        print(f"⚠️  [商拍子系统] LangGraph checkpointer 初始化失败（graph 相关功能降级）: {exc}")
        _checkpointer = None
        _checkpointer_cm = None

    # 编译 graph（无论 checkpointer 可用与否）
    try:
        from wellflow.app.workflows.parent_graph import build_graph
        _graph = build_graph(checkpointer=_checkpointer)
        if _checkpointer:
            print("✅ [商拍子系统] LangGraph 父图编译完成（带 checkpointer）")
        else:
            print("✅ [商拍子系统] LangGraph 父图编译完成（无 checkpointer）")
    except Exception as exc:
        print(f"⚠️  [商拍子系统] LangGraph 父图编译失败: {exc}")
        _graph = None


# ---------------------------------------------------------------------------
# Lifespan：启动时初始化 LangGraph checkpointer
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动：初始化 AsyncPostgresSaver + LangGraph checkpoint 表。"""
    global _checkpointer, _checkpointer_cm
    await init_wellflow_runtime()
    try:
        yield
    finally:
        if _checkpointer_cm is not None:
            try:
                await _checkpointer_cm.__aexit__(None, None, None)
            except Exception:
                pass
            _checkpointer_cm = None
        _checkpointer = None
        print("🛑 [商拍子系统] 应用关闭，释放 checkpointer 连接池")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="WellFlow 电商商拍平台 API",
    description=(
        "商拍任务编排 API。"
        "详见 final.md 架构文档。"
    ),
    version="0.2.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# CORS（开发用，生产需限制）
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 全局异常处理（统一转为 {code, data, message}）
# ---------------------------------------------------------------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        message = exc.detail.get("message") or str(exc.detail)
        data = {k: v for k, v in exc.detail.items() if k != "message"} or None
    else:
        message = str(exc.detail) if exc.detail else "请求错误"
        data = None
    return JSONResponse(
        status_code=exc.status_code,
        content=fail(message=message, code=exc.status_code, data=data),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    simplified = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        simplified.append({"loc": loc, "msg": err.get("msg", ""), "type": err.get("type", "")})
    return JSONResponse(
        status_code=422,
        content=fail(message="参数校验失败", code=422, data=simplified),
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content=fail(message=f"服务内部错误: {exc}", code=500),
    )


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------

@app.get("/health", tags=["系统"], summary="健康检查", response_model=StandardResponse[dict])
def health():
    return ok({
        "status": "ok",
        "version": app.version,
        "gateway": "new-api",
        "langgraph_available": _graph is not None,
        "checkpointer_available": _checkpointer is not None,
    })


# ---------------------------------------------------------------------------
# 挂载业务路由
# ---------------------------------------------------------------------------

# 任务 API
app.include_router(tasks_router, prefix="/api")

# SKU 商品库（品牌 / 系列 / SKU）
app.include_router(products_router, prefix="/api")

# 模特库（参考素材）
app.include_router(mannequins_router, prefix="/api")

# WellFlow 通用图片上传（不绑定 task，供 SKU/模特/任意素材库先传后提）
app.include_router(uploads_router, prefix="/api")

# 对话入口（意图路由 —— 把自然语言映射到 LangGraph 节点）
app.include_router(chat_router, prefix="/api")

# 会话（Conversation-centric：左侧历史 + 消息持久化）
app.include_router(conversations_router, prefix="/api")

# SSE
app.include_router(sse_router)


# ---------------------------------------------------------------------------
# 静态图片服务：uploads/ 下的上传图 + 生图成品，通过 /uploads/{...} 直接访问
# ---------------------------------------------------------------------------

_upload_dir = Path(settings.upload_dir).resolve()
_upload_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=_upload_dir), name="uploads")
