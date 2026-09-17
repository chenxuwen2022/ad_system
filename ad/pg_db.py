# -*- coding: utf-8 -*-
"""PostgreSQL 投放记录表（广告子系统独立数据模块）。

链接项目 PostgreSQL（读 .env 的 DATABASE_URL，用户提供：
postgresql+psycopg2://postgres:postgresSY123456@localhost:5432/wellflow），
创建并维护投放记录表 launch_record。PG 不可用时自动降级（仅记录，不影响广告投放主流程）。
"""
import datetime
import os

from sqlalchemy import create_engine, Column, String, Float, DateTime, BigInteger
from sqlalchemy.orm import declarative_base, sessionmaker

from ad.config import BASE_DIR

# 兼容：从 ad/.env 或根 .env 读取 DATABASE_URL
def _load_env():
    for p in (os.path.join(BASE_DIR, ".env"),
              os.path.join(os.path.dirname(BASE_DIR), ".env")):
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgresSY123456@localhost:5432/wellflow",
)

BasePG = declarative_base()


class LaunchRecordDB(BasePG):
    """投放记录表：每次投放（真实/测试）记录一条，落 PostgreSQL。"""
    __tablename__ = "launch_record"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    advertiser_id = Column(String(64), default="", index=True)   # 投放店铺（广告主ID）
    file_path = Column(String(500), default="", index=True)      # 素材本地路径
    status = Column(String(16), default="success")               # success / fail
    mode = Column(String(16), default="real")                    # real / test
    plan_id = Column(String(64), default="")                     # 投放计划ID
    plan_name = Column(String(255), default="")                  # 投放计划名
    product_id = Column(String(255), default="")                 # 商品ID（多个逗号分隔）
    budget = Column(Float, default=0)                            # 预算（元）
    detail = Column(String(1000), default="")                    # 结果详情/错误信息
    create_time = Column(DateTime, default=datetime.datetime.now, index=True)


# PG 引擎（连接失败不阻断广告主流程，仅降级为不可写）
pg_engine = None
PG_SessionLocal = None
_pg_ok = False
try:
    pg_engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
    PG_SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=pg_engine)
    _pg_ok = True
except Exception as e:
    print(f"[ad/pg] PostgreSQL 连接初始化失败（投放记录表不可写，不影响主流程）: {e}")


def init_pg_db():
    """创建投放记录表（已存在则跳过）。失败打印警告并降级。"""
    global _pg_ok
    if not _pg_ok:
        return False
    try:
        BasePG.metadata.create_all(bind=pg_engine)
        print(f"[ad/pg] ✅ 投放记录表 launch_record 就绪（{DATABASE_URL.rsplit('@', 1)[-1]}）")
        return True
    except Exception as e:
        _pg_ok = False
        print(f"[ad/pg] 建表失败（降级，不影响主流程）: {e}")
        return False


def save_launch_record(**kwargs):
    """写入一条投放记录到 PostgreSQL。失败静默（不影响投放主流程）。"""
    if not _pg_ok or PG_SessionLocal is None:
        return False
    try:
        db = PG_SessionLocal()
        try:
            db.add(LaunchRecordDB(**kwargs))
            db.commit()
            return True
        finally:
            db.close()
    except Exception as e:
        print(f"[ad/pg] 写入投放记录失败: {e}")
        return False


def query_launch_records(limit: int = 100, advertiser_id: str = ""):
    """查询投放记录（倒序）。PG 不可用时返回 []。"""
    if not _pg_ok or PG_SessionLocal is None:
        return []
    try:
        db = PG_SessionLocal()
        try:
            q = db.query(LaunchRecordDB)
            if advertiser_id:
                q = q.filter(LaunchRecordDB.advertiser_id == advertiser_id)
            rows = q.order_by(LaunchRecordDB.id.desc()).limit(limit).all()
            return [{
                "id": r.id, "advertiser_id": r.advertiser_id,
                "file_path": r.file_path, "status": r.status, "mode": r.mode,
                "plan_id": r.plan_id, "plan_name": r.plan_name,
                "product_id": r.product_id, "budget": r.budget,
                "detail": r.detail,
                "create_time": r.create_time.strftime("%Y-%m-%d %H:%M:%S") if r.create_time else "",
            } for r in rows]
        finally:
            db.close()
    except Exception as e:
        print(f"[ad/pg] 查询投放记录失败: {e}")
        return []
