from contextlib import contextmanager
from typing import AsyncIterator

from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from wellflow.app.config import settings

# ---------------------------------------------------------------------------
# 同步 engine —— ad 系统 + wellflow 其他 CRUD 用（sync Session）
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
# 异步 engine —— 给 conversations / chat / SSE 这些并发敏感的端点用
# （独立于 sync engine，ad 系统不受影响）
# ---------------------------------------------------------------------------
async_engine: AsyncEngine = create_async_engine(
    settings.database_url_async,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    connect_args={"server_settings": {"timezone": "UTC"}},  # asyncpg 侧设置会话时区
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


def get_db():
    """FastAPI 依赖注入：获取同步数据库会话（给 sync def 接口用）"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_async_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入：获取异步数据库会话（给 async def 接口用）"""
    async with AsyncSessionLocal() as db:
        yield db


@contextmanager
def session_scope():
    """手动开启并及时释放 Session（后台线程 fire-and-forget 写库用）。

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
