from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from wellflow.app.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
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


class Base(DeclarativeBase):
    """SQLAlchemy 2.x 声明式基类"""
    pass


def get_db():
    """FastAPI 依赖注入：获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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


# ---------------------------------------------------------------------------
# 异步会话（方案 B：async 端点内的「立即入库」走 AsyncSession；
# 后台 daemon 线程与其余同步模块继续用上面的同步链路，互不影响）
# ---------------------------------------------------------------------------
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession, async_sessionmaker, create_async_engine,
)

# URL 兜底:未配置 database_url_async 时,自动从同步 URL 派生(psycopg2 → asyncpg),
# 保证任何环境(含同事本地)零配置可用
_async_url = settings.database_url_async or settings.database_url.replace(
    "postgresql+psycopg2://", "postgresql+asyncpg://", 1)
async_engine = create_async_engine(
    _async_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(bind=async_engine, expire_on_commit=False)


async def get_db_async():
    """FastAPI 依赖注入：async 端点用（每次请求独立异步会话）。"""
    async with AsyncSessionLocal() as db:
        yield db
