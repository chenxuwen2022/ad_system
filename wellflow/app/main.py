"""WellFlow 后端 - FastAPI 应用入口"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from wellflow.app.api.tasks import router as tasks_router
from wellflow.app.sse import router as sse_router
from wellflow.app.api.utils import fail
from wellflow.app.config import settings


# ---------------------------------------------------------------------------
# 全局单例：checkpointer + graph（在 lifespan / 宿主启动时初始化）
# ---------------------------------------------------------------------------

_checkpointer: Any = None
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
    global _checkpointer, _graph

    _checkpointer = None
    _graph = None
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        conn_string = settings.database_url_async.replace(
            "postgresql+asyncpg://", "postgresql://"
        )
        cm = AsyncPostgresSaver.from_conn_string(conn_string)
        saver = await cm.__aenter__()  # 进入连接池生命周期（应用运行期间保持）
        await saver.setup()
        _checkpointer = saver
        print("✅ [商拍子系统] AsyncPostgresSaver 初始化完成，checkpoint 表已就绪")

        # 编译 graph（在 saver 连接池存活期间）
        try:
            from wellflow.app.workflows.parent_graph import build_graph
            _graph = build_graph(checkpointer=_checkpointer)
            print("✅ [商拍子系统] LangGraph 父图编译完成")
        except Exception as exc:
            print(f"⚠️  [商拍子系统] LangGraph 父图编译失败: {exc}")
            _graph = None
    except Exception as exc:
        print(f"⚠️  [商拍子系统] LangGraph checkpointer 初始化失败（graph 相关功能降级）: {exc}")
        import traceback
        traceback.print_exc()
        _checkpointer = None
        _graph = None


# ---------------------------------------------------------------------------
# Lifespan：启动时初始化 LangGraph checkpointer
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动：初始化 AsyncPostgresSaver + LangGraph checkpoint 表。"""
    await init_wellflow_runtime()
    try:
        yield
    finally:
        if _checkpointer is not None:
            try:
                await _checkpointer.__aexit__(None, None, None)
            except Exception:
                pass
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
# 全局异常处理
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content=fail(message=f"服务内部错误: {str(exc)}", code=500),
    )


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------

@app.get("/health", tags=["系统"], summary="健康检查")
def health():
    return {
        "status": "ok",
        "version": app.version,
        "gateway": "new-api",
        "langgraph_available": _graph is not None,
        "checkpointer_available": _checkpointer is not None,
    }


# ---------------------------------------------------------------------------
# 挂载业务路由
# ---------------------------------------------------------------------------

# 任务 API
app.include_router(tasks_router, prefix="/api")

# SSE
app.include_router(sse_router)


# ---------------------------------------------------------------------------
# 静态图片服务：uploads/ 下的上传图 + 生图成品，通过 /uploads/{...} 直接访问
# ---------------------------------------------------------------------------

_upload_dir = Path(settings.upload_dir).resolve()
_upload_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=_upload_dir), name="uploads")
