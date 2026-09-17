"""WellFlow 运行时初始化 + LangGraph 单例。

职责：
- 持有 LangGraph checkpointer + graph 的全局单例
- 提供 init / getter 供宿主应用或测试代码调用

纯逻辑模块，**不依赖 FastAPI**，可被任意宿主（根目录 main.py、测试脚本）import。
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# 全局单例（在宿主 lifespan / startup 时调用 init_wellflow_runtime 填充）
# ---------------------------------------------------------------------------

_checkpointer: Any = None
_checkpointer_cm: Any = None  # 保留连接池 context manager 引用，防止 GC 关闭连接
_graph: Any = None


def get_checkpointer():
    return _checkpointer


def get_graph():
    return _graph


# ---------------------------------------------------------------------------
# 初始化：AsyncPostgresSaver + LangGraph 父图
# ---------------------------------------------------------------------------


async def init_wellflow_runtime() -> None:
    """初始化 checkpointer 与 graph。

    两种使用场景：
    1. 宿主应用（根目录 main.py 统一入口）的 lifespan 调用
    2. 测试脚本 / 独立 CLI 入口手动调用

    初始化失败时降级（_checkpointer/_graph = None），不影响其余 API。
    """
    global _checkpointer, _checkpointer_cm, _graph

    _checkpointer = None
    _graph = None
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from wellflow.app.config import settings

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


async def shutdown_wellflow_runtime() -> None:
    """释放 checkpointer 连接池（宿主 lifespan finally 调用）。"""
    global _checkpointer, _checkpointer_cm
    if _checkpointer_cm is not None:
        try:
            await _checkpointer_cm.__aexit__(None, None, None)
        except Exception:
            pass
        _checkpointer_cm = None
    _checkpointer = None
    print("🛑 [商拍子系统] 运行时已关闭")
