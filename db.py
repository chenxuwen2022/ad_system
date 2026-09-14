from sqlalchemy import create_engine, Column, String, Integer, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
import datetime
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SQLITE_DB_PATH = os.path.join(BASE_DIR, "token_store.db")

engine = create_engine(f"sqlite:///{SQLITE_DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class TokenDB(Base):
    __tablename__ = "douyin_token"

    id = Column(Integer, primary_key=True, index=True)
    access_token = Column(String, nullable=False)
    refresh_token = Column(String, nullable=False)
    expires_in = Column(Integer, nullable=False)
    refresh_expires_in = Column(Integer, nullable=False)
    update_time = Column(DateTime, default=datetime.datetime.now)


class AdvertiserDB(Base):
    """后台保存的广告主账户列表（设置里可增删改）"""
    __tablename__ = "advertiser_account"

    id = Column(Integer, primary_key=True, index=True)
    advertiser_id = Column(String, nullable=False, unique=True)
    name = Column(String, nullable=False, default="")
    update_time = Column(DateTime, default=datetime.datetime.now)


class MaterialTagDB(Base):
    """素材标签：后台维护的标签列表（标签设置里增删改查）"""
    __tablename__ = "material_tag"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True)
    color = Column(String, nullable=False, default="#1f6feb")
    create_time = Column(DateTime, default=datetime.datetime.now)
    update_time = Column(DateTime, default=datetime.datetime.now)


class MaterialMarkDB(Base):
    """素材标记：投放时给素材打的标签，按文件路径关联（一个素材多个标签，逗号分隔）"""
    __tablename__ = "material_mark"

    id = Column(Integer, primary_key=True, index=True)
    file_path = Column(String, nullable=False, unique=True)
    tags = Column(String, nullable=False, default="")
    create_time = Column(DateTime, default=datetime.datetime.now)
    update_time = Column(DateTime, default=datetime.datetime.now)


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
