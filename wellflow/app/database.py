from contextlib import contextmanager
from typing import AsyncIterator

from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from wellflow.app.config import settings

# ---------------------------------------------------------------------------
# 同步 engine —— ad 系统 + wellflow sync 端点（psycopg2 驱动）
# ---------------------------------------------------------------------------
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)


@event.listens_for(engine, "connect")
def _set_pg_timezone(dbapi_connection, connection_record):
    """强制所有 Postgres 会话用 UTC。

    timestamptz 内部存的是 UTC 绝对时刻，SQLAlchemy 按会话时区读回。
    不设会话时区的话读回来就是 PG server 的时区（我们本地是 Asia/Shanghai +08），
    isoformat() 输出带 +08:00。设成 UTC 后读回就是 aware UTC，
    isoformat() 永远带 +00:00，API 输出不再耦合 PG server 的时区配置。
    """
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("SET TIME ZONE 'UTC'")
        cursor.close()
    except Exception:
        # 非 PG 后端（比如 SQLite 用作测试）会忽略这个事件
        pass


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# ---------------------------------------------------------------------------
# 异步 engine —— conversations / chat / outfit（asyncpg 驱动）
#
# URL 兜底：未配置 database_url_async 时自动从同步 URL 派生
# （psycopg2 → asyncpg），保证任何环境零配置可用。
# （之前此文件有两处 async engine 定义，顶部一处 import 时就会初始化 pool
#  占 PG 连接但从不被使用，底部一处覆盖了它但丢了 connect_args 时区。
#  现已合并为单一定义。）
# ---------------------------------------------------------------------------
_async_url = settings.database_url_async or settings.database_url.replace(
    "postgresql+psycopg2://", "postgresql+asyncpg://", 1
)
async_engine: AsyncEngine = create_async_engine(
    _async_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    connect_args={"server_settings": {"timezone": "UTC"}},  # asyncpg 侧强制 UTC，与 sync engine 的 connect event 对齐
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    async_engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """SQLAlchemy 2.x 声明式基类"""
    pass


# ---------------------------------------------------------------------------
# 依赖注入 —— FastAPI Depends 用
# ---------------------------------------------------------------------------

def get_db():
    """FastAPI 依赖注入：同步端点用（每次请求独立 Session）。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_async_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入：async 端点用（conversations.py 引用此名）。"""
    async with AsyncSessionLocal() as db:
        yield db


async def get_db_async() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入：async 端点用（outfit.py 引用此名）。

    与 get_async_db 完全等价。保留两个别名是为了兼容历史 import。
    """
    async with AsyncSessionLocal() as db:
        yield db


# ---------------------------------------------------------------------------
# 手动 Session 管理 —— 后台线程 / LangGraph checkpoint 等无 FastAPI 依赖的场景
# ---------------------------------------------------------------------------

@contextmanager
def session_scope():
    """手动开启并及时释放同步 Session（后台线程 fire-and-forget 写库用）。

    与 get_db() 不同：get_db() 是 FastAPI 依赖注入生成器，不能用 next()
    取一次就丢弃——那样 finally 里的 db.close() 不会执行，导致 Session 泄漏。
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
