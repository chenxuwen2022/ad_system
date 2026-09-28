"""WellFlow 运行时初始化 + LangGraph 单例。

职责：
- 持有 LangGraph checkpointer + graph 的全局单例
- 提供 init / getter 供宿主应用或测试代码调用

纯逻辑模块，**不依赖 FastAPI**，可被任意宿主（根目录 main.py、测试脚本）import。
"""

from __future__ import annotations

from wellflow.app.logging import log_message

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
        from psycopg_pool import AsyncConnectionPool
        from wellflow.app.config import settings

        conn_string = settings.database_url_async.replace(
            "postgresql+asyncpg://", "postgresql://"
        )
        # PG 不可用时快速降级，避免启动卡在连接超时
        if "?" in conn_string:
            conn_string += "&connect_timeout=5"
        else:
            conn_string += "?connect_timeout=5"

        # 🔑 关键：用 AsyncConnectionPool 而不是 AsyncPostgresSaver.from_conn_string。
        # from_conn_string 内部只建一个 AsyncConnection，
        # LangGraph graph.astream / aget_state / aput 多个协程并发访问时，
        # psycopg/asyncpg 会报 "another command is already in progress" /
        # "cannot enter pipeline mode, connection not idle"。
        # 连接池让每个并发请求拿到独立连接，彻底解决单连接并发冲突。
        pool = AsyncConnectionPool(
            conn_string,
            min_size=4,
            max_size=16,
            open=True,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        await pool.wait()

        saver = AsyncPostgresSaver(conn=pool)
        await saver.setup()
        _checkpointer = saver
        _checkpointer_cm = pool  # 保留 pool 引用，shutdown 时 close
        log_message("✅ [商拍子系统] AsyncPostgresSaver(连接池 min=4 max=16) 初始化完成，checkpoint 表已就绪", page='系统', business='服务生命周期', status='成功')
    except Exception as exc:
        log_message(f"⚠️  [商拍子系统] LangGraph checkpointer 初始化失败（graph 相关功能降级）: {exc}", page='系统', business='服务生命周期', status='警告')
        _checkpointer = None
        _checkpointer_cm = None

    # 编译 graph（无论 checkpointer 可用与否）
    try:
        from wellflow.app.workflows.parent_graph import build_graph
        if _checkpointer is None:
            raise RuntimeError("持久化 checkpoint 不可用，禁止启动不可恢复的工作流")
        _graph = build_graph(checkpointer=_checkpointer)
        if _checkpointer:
            log_message("✅ [商拍子系统] LangGraph 父图编译完成（带 checkpointer）", page='系统', business='服务生命周期', status='成功')
        else:
            log_message("✅ [商拍子系统] LangGraph 父图编译完成（无 checkpointer）", page='系统', business='服务生命周期', status='成功')
    except Exception as exc:
        log_message(f"⚠️  [商拍子系统] LangGraph 父图编译失败: {exc}", page='系统', business='服务生命周期', status='警告')
        _graph = None


async def shutdown_wellflow_runtime() -> None:
    """释放 checkpointer 连接池（宿主 lifespan finally 调用）。"""
    global _checkpointer, _checkpointer_cm
    if _checkpointer_cm is not None:
        try:
            # 可能是 AsyncConnectionPool（新）或 asynccontextmanager（旧，兼容）
            if hasattr(_checkpointer_cm, "close"):
                await _checkpointer_cm.close()
            else:
                await _checkpointer_cm.__aexit__(None, None, None)
        except Exception:
            pass
        _checkpointer_cm = None
    _checkpointer = None
    log_message("🛑 [商拍子系统] 运行时已关闭", page='系统', business='服务生命周期', status='记录')
