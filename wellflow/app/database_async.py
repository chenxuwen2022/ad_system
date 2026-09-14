"""异步数据库基础设施：供 LangGraph AsyncPostgresSaver 和 async 仓储使用。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from wellflow.app.config import settings


# 只有当配置了异步 URL 时才初始化 async engine，避免 SQLite 同步场景崩溃
_async_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_async_engine() -> AsyncEngine | None:
    global _async_engine
    if _async_engine is None:
        url = settings.database_url_async
        if not url or "asyncpg" not in url:
            return None
        _async_engine = create_async_engine(url, pool_pre_ping=True)
    return _async_engine


def get_async_session_factory() -> async_sessionmaker[AsyncSession] | None:
    global _async_session_factory
    if _async_session_factory is None:
        engine = get_async_engine()
        if engine is None:
            return None
        _async_session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return _async_session_factory


async def get_async_session():
    """FastAPI async 依赖注入。"""
    factory = get_async_session_factory()
    if factory is None:
        raise RuntimeError("database_url_async 未配置 asyncpg 驱动")
    async with factory() as session:
        yield session
