import datetime
import os

import requests
from sqlalchemy import create_engine, Column, String, Integer, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

from ad.engine_config import ENGINE_CONFIG

# 巨量营销独立 token 存储：使用独立 sqlite 文件，与千川 token_store.db 完全隔离
SQLITE_DB_PATH = ENGINE_CONFIG["TOKEN_DB_PATH"]

_engine = create_engine(f"sqlite:///{SQLITE_DB_PATH}", connect_args={"check_same_thread": False})
_SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
_Base = declarative_base()


class EngineTokenDB(_Base):
    __tablename__ = "engine_token"

    id = Column(Integer, primary_key=True, index=True)
    access_token = Column(String, nullable=False)
    refresh_token = Column(String, nullable=False)
    expires_in = Column(Integer, nullable=False)
    refresh_expires_in = Column(Integer, nullable=False)
    advertiser_ids = Column(String, nullable=False, default="")  # JSON 数组
    update_time = Column(DateTime, default=datetime.datetime.now)


def _init_db():
    _Base.metadata.create_all(bind=_engine)


class EngineTokenManager:
    """巨量营销 token 管理器（与千川 DouYinTokenManager 完全独立）"""

    def __init__(self):
        _init_db()
        self.db = _SessionLocal()
        self.token_info: EngineTokenDB | None = None
        self._load()

    def _load(self):
        self.token_info = self.db.query(EngineTokenDB).first()

    def save_token(self, data: dict):
        now = datetime.datetime.now()
        payload = {
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "expires_in": int(data.get("expires_in") or 0),
            "refresh_expires_in": int(data.get("refresh_token_expires_in") or data.get("refresh_expires_in") or 0),
            "advertiser_ids": str(data.get("advertiser_ids") or []),
            "update_time": now,
        }
        if self.token_info:
            for k, v in payload.items():
                if v is not None:
                    setattr(self.token_info, k, v)
        else:
            self.token_info = EngineTokenDB(**payload)
            self.db.add(self.token_info)
        self.db.commit()

    def is_expire(self) -> bool:
        if not self.token_info:
            return True
        past = (datetime.datetime.now() - self.token_info.update_time).total_seconds()
        return past >= (self.token_info.expires_in or 0) - 120

    def get_access_token(self) -> str:
        self._load()
        if self.is_expire():
            self.refresh()
        if not self.token_info or not self.token_info.access_token:
            raise Exception("巨量营销 token 未授权，请先使用 auth_code 授权")
        return self.token_info.access_token

    def request_first_token(self, auth_code: str):
        """首次授权：auth_code 换 token"""
        payload = {
            "app_id": ENGINE_CONFIG["APP_ID"],
            "secret": ENGINE_CONFIG["Secret"],
            "grant_type": "auth_code",
            "auth_code": auth_code,
        }
        res = requests.post(ENGINE_CONFIG["token_url"], json=payload, timeout=15)
        res.raise_for_status()
        resp_json = res.json()
        if resp_json.get("code") != 0:
            raise Exception(f"获取巨量营销token失败:{resp_json}")
        data = resp_json.get("data", {})
        self.save_token(data)
        return data.get("advertiser_ids", [])

    def refresh(self):
        if not self.token_info or not self.token_info.refresh_token:
            raise Exception("无 refresh_token，请先使用 auth_code 授权")
        payload = {
            "app_id": ENGINE_CONFIG["APP_ID"],
            "secret": ENGINE_CONFIG["Secret"],
            "grant_type": "refresh_token",
            "refresh_token": self.token_info.refresh_token,
        }
        resp = requests.post(ENGINE_CONFIG["refresh_token_url"], json=payload, timeout=15)
        resp_json = resp.json()
        if resp_json.get("code") != 0:
            raise Exception(f"刷新巨量营销token失败:{resp_json}")
        self.save_token(resp_json.get("data", {}))

    def get_advertiser_ids(self) -> list:
        """返回已授权的广告主账户列表"""
        token = self.get_access_token()
        resp = requests.get(
            ENGINE_CONFIG["advertiser_get_url"],
            params={"access_token": token},
            headers={"Access-Token": token},
            timeout=10,
        )
        resp_json = resp.json()
        if resp_json.get("code") != 0:
            raise Exception(f"获取巨量营销广告主列表失败:{resp_json}")
        return resp_json.get("data", {}).get("list", [])


engine_token_mgr: EngineTokenManager | None = None


def get_engine_token_mgr() -> EngineTokenManager:
    global engine_token_mgr
    if engine_token_mgr is None:
        engine_token_mgr = EngineTokenManager()
    return engine_token_mgr
