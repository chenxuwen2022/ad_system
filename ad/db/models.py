import datetime
import os

from sqlalchemy import create_engine, Column, String, Integer, DateTime, text
from sqlalchemy.orm import declarative_base, sessionmaker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# 兼容：从 ad/.env 或根 .env 读取数据库连接（与 pg_db.py 同源）
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

# 所有业务数据统一落到远程 PostgreSQL（wellflow 库）
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgresSY123456@192.168.110.254:5432/wellflow",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class TokenDB(Base):
    """千川 access/refresh token"""
    __tablename__ = "douyin_token"

    id = Column(Integer, primary_key=True, autoincrement=True)
    access_token = Column(String, nullable=False)
    refresh_token = Column(String, nullable=False)
    expires_in = Column(Integer, nullable=False)
    refresh_expires_in = Column(Integer, nullable=False)
    update_time = Column(DateTime, default=datetime.datetime.now)


class AdvertiserDB(Base):
    """后台保存的广告主账户列表（设置里可增删改）"""
    __tablename__ = "advertiser_account"

    id = Column(Integer, primary_key=True, autoincrement=True)
    advertiser_id = Column(String, nullable=False, unique=True)
    name = Column(String, nullable=False, default="")
    update_time = Column(DateTime, default=datetime.datetime.now)


class MaterialTagDB(Base):
    """素材标签：后台维护的标签列表（标签设置里增删改查）"""
    __tablename__ = "material_tag"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    color = Column(String, nullable=False, default="#1f6feb")
    tag_type = Column(String, nullable=False, default="public")  # public=公共标签 / personal=个人标签
    create_time = Column(DateTime, default=datetime.datetime.now)
    update_time = Column(DateTime, default=datetime.datetime.now)


class MaterialCategoryDB(Base):
    """素材分类体系：主类目(level=1) → 一级分类(level=2) → 二级分类(level=3)，parent_id 自关联"""
    __tablename__ = "material_category"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    level = Column(Integer, nullable=False, default=1)            # 1/2/3
    parent_id = Column(Integer, nullable=False, default=0)       # 父分类ID，0=主类目
    sort = Column(Integer, nullable=False, default=0)
    create_time = Column(DateTime, default=datetime.datetime.now)
    update_time = Column(DateTime, default=datetime.datetime.now)


class MaterialBizStatusDB(Base):
    """素材投放状态标签：可增删改，name=显示名，code=状态编码（不变），color=色块"""
    __tablename__ = "material_biz_status"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)          # 显示名（可改）
    code = Column(String, nullable=False, unique=True)  # 状态编码（新增后不变）
    color = Column(String, nullable=False, default="#CCFF00")
    sort = Column(Integer, nullable=False, default=0)
    create_time = Column(DateTime, default=datetime.datetime.now)
    update_time = Column(DateTime, default=datetime.datetime.now)


class MaterialMarkDB(Base):
    """素材标记：投放时给素材打的标签，按文件路径关联（一个素材多个标签，逗号分隔）"""
    __tablename__ = "material_mark"

    id = Column(Integer, primary_key=True, autoincrement=True)
    file_path = Column(String, nullable=False, unique=True)
    tags = Column(String, nullable=False, default="")
    create_time = Column(DateTime, default=datetime.datetime.now)
    update_time = Column(DateTime, default=datetime.datetime.now)


class MaterialLaunchDB(Base):
    """素材投放历史：每次投放记录一条，用于素材库展示每个素材的投放状态"""
    __tablename__ = "material_launch"

    id = Column(Integer, primary_key=True, autoincrement=True)
    file_path = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="success")  # success / fail
    mode = Column(String, nullable=False, default="real")       # real / test
    biz_status = Column(String, nullable=False, default="")     # 业务状态：待审核/审核驳回/通过-待投放/直播间已投放/商城已投放/已投放商品+直播间/放弃测试
    plan_id = Column(String, nullable=False, default="")
    plan_name = Column(String, nullable=False, default="")
    product_id = Column(String, nullable=False, default="")
    detail = Column(String, nullable=False, default="")
    material_id = Column(String, nullable=False, default="")     # 千川素材ID，用于同步状态
    advertiser_id = Column(String, nullable=False, default="")   # 广告主ID
    reject_reason = Column(String, nullable=False, default="")   # 审核驳回原因
    create_time = Column(DateTime, default=datetime.datetime.now)



class SkuDB(Base):
    """图片裂变 SKU 表：系列、货号、商品名称、品牌、类目"""
    __tablename__ = "image_split_sku"

    id = Column(Integer, primary_key=True, autoincrement=True)
    series = Column(String, nullable=False, default="")
    sku_no = Column(String, nullable=False, default="")
    name = Column(String, nullable=False, default="")
    brand = Column(String, nullable=False, default="")
    category = Column(String, nullable=False, default="")
    create_time = Column(DateTime, default=datetime.datetime.now)
    update_time = Column(DateTime, default=datetime.datetime.now)


class MaterialUploadDB(Base):
    """素材上传记录：每上传一条素材记录一条，用于审计"""
    __tablename__ = "material_upload"

    id = Column(Integer, primary_key=True, autoincrement=True)
    file_name = Column(String, nullable=False, default="")       # 文件名
    file_path = Column(String, nullable=False, default="")       # 文件路径
    file_type = Column(String, nullable=False, default="")        # 类型：image / video
    file_size = Column(Integer, nullable=False, default=0)       # 文件大小（字节）
    upload_ip = Column(String, nullable=False, default="")        # 上传人 IP
    upload_user = Column(String, nullable=False, default="")      # 上传人（没有就为空）
    create_time = Column(DateTime, default=datetime.datetime.now)  # 上传时间

# 素材投放业务状态枚举（页面徽标 + 手动标注共用）
MATERIAL_BIZ_STATUSES = [
    "待审核",
    "审核驳回",
    "通过-待投放",
    "直播间已投放",
    "商城已投放",
    "已投放商品+直播间",
    "放弃测试",
]

# 各业务状态 → 徽标样式类（与 upload_routes.py 中 .launch-badge.* 对应）
MATERIAL_BIZ_STATUS_CLASS = {
    "待审核": "pending",
    "审核驳回": "fail",
    "通过-待投放": "ready",
    "直播间已投放": "live",
    "商城已投放": "shop",
    "已投放商品+直播间": "both",
    "放弃测试": "abandon",
}


def init_db():
    """创建全部业务表到 PostgreSQL（已存在则跳过）。连接失败打印警告。"""
    try:
        Base.metadata.create_all(bind=engine)
        host = DATABASE_URL.rsplit("@", 1)[-1]
        print(f"[ad/db] ✅ 业务表已就绪（PostgreSQL: {host}）")
    except Exception as e:
        print(f"[ad/db] ⚠️ 建表失败（降级，不影响主流程）: {e}")
    # 同时初始化投放记录表（launch_record）
    try:
        from ad.pg_db import init_pg_db
        init_pg_db()
    except Exception as e:
        print(f"[ad/db] ⚠️ pg_db 初始化失败: {e}")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
