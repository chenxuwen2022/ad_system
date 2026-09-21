"""
店铺广告主账户设置 CRUD
挂载: main.py 里 app.include_router(shops_router, prefix="/api/shops")
依赖: DATABASE_URL_ASYNC=postgresql+asyncpg://postgres:***@192.168.110.254:5432/wellflow
"""
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import select, update, delete
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
import os

# ---- DB ----
DATABASE_URL = os.getenv(
    "DATABASE_URL_ASYNC",
    "postgresql+asyncpg://postgres:postgresSY123456@192.168.110.254:5432/wellflow",
)
engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

class Base(DeclarativeBase):
    pass

class AdShop(Base):
    __tablename__ = "ads_shops"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(default="巨量千川")      # 巨量千川/巨量营销/腾讯广告/小红书聚光
    advertiser_id: Mapped[str] = mapped_column(index=True)         # 广告主 ID
    shop_name: Mapped[str] = mapped_column()                       # 店铺名称
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)

async def get_db():
    async with SessionLocal() as s:
        yield s

async def init_ads_shops():
    """启动时建表（已存在则跳过）"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

router = APIRouter(tags=["ads-shops"])

class ShopIn(BaseModel):
    channel: str = "巨量千川"
    advertiser_id: str
    shop_name: str

@router.get("")
async def list_shops(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(AdShop).order_by(AdShop.id.desc()))).scalars().all()
    return [{"id": r.id, "channel": r.channel, "advertiser_id": r.advertiser_id,
             "shop_name": r.shop_name} for r in rows]

@router.post("")
async def create_shop(body: ShopIn, db: AsyncSession = Depends(get_db)):
    exists = (await db.execute(select(AdShop).where(AdShop.advertiser_id == body.advertiser_id))).scalar_one_or_none()
    if exists:
        raise HTTPException(400, f"广告主 ID {body.advertiser_id} 已存在")
    s = AdShop(**body.dict())
    db.add(s); await db.commit(); await db.refresh(s)
    return {"id": s.id}

@router.put("/{shop_id}")
async def update_shop(shop_id: int, body: ShopIn, db: AsyncSession = Depends(get_db)):
    s = (await db.get(AdShop, shop_id))
    if not s:
        raise HTTPException(404, "账户不存在")
    s.channel = body.channel
    s.advertiser_id = body.advertiser_id
    s.shop_name = body.shop_name
    await db.commit()
    return {"ok": True}

@router.delete("/{shop_id}")
async def delete_shop(shop_id: int, db: AsyncSession = Depends(get_db)):
    await db.execute(delete(AdShop).where(AdShop.id == shop_id))
    await db.commit()
    return {"ok": True}
#（注：内容由AI生成）

