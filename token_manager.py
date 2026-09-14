import datetime

import requests

from config import DOUYIN_CONFIG
from db import SessionLocal, TokenDB, init_db


class DouYinTokenManager:
    def __init__(self):
        init_db()  # 确保数据表已创建，避免查询时报 no such table
        self.db = SessionLocal()
        self.token_info: TokenDB | None = None
        self._load_from_db()

    def _load_from_db(self):
        """从数据库加载token"""
        self.token_info = self.db.query(TokenDB).first()

    def save_token_to_db(self, resp_data: dict):
        """把token写入/更新sqlite，只更新响应中存在的字段，缺失字段保留原值"""
        now = datetime.datetime.now()

        def _to_int(v):
            return int(v) if v is not None else None

        payload = {
            "access_token": resp_data.get("access_token"),
            "refresh_token": resp_data.get("refresh_token"),
            "expires_in": _to_int(resp_data.get("expires_in")),
            # 巨量引擎响应的字段名是 refresh_token_expires_in，旧字段兼容保留
            "refresh_expires_in": _to_int(resp_data.get("refresh_token_expires_in") or resp_data.get("refresh_expires_in")),
            "update_time": now,
        }
        record = self.db.query(TokenDB).first()
        if record:
            for k, v in payload.items():
                if v is not None:
                    setattr(record, k, v)
        else:
            record = TokenDB(**{k: v for k, v in payload.items() if v is not None})
            self.db.add(record)
        self.db.commit()
        self.token_info = record

    def is_access_token_expire(self) -> bool:
        if not self.token_info:
            return True
        past = (datetime.datetime.now() - self.token_info.update_time).total_seconds()
        # 提前120秒判定过期，避免临界时间失效
        return past >= self.token_info.expires_in - 120

    def get_access_token(self) -> str:
        self._load_from_db()
        if self.is_access_token_expire():
            self.refresh_access_token()
        return self.token_info.access_token

    @staticmethod
    def _check_error(resp_json: dict, action: str):
        """巨量引擎开放平台返回码在顶层 code 字段，0 为成功"""
        if resp_json.get("code") != 0:
            raise Exception(f"{action}失败:{resp_json}")

    def request_first_token(self, auth_code: str) -> list:
        """首次授权，用 auth_code 换取 token 存入数据库，返回已授权的广告主ID列表"""
        payload = {
            "app_id": DOUYIN_CONFIG["APP_ID"],
            "secret": DOUYIN_CONFIG["Secret"],
            "auth_code": auth_code,
        }
        res = requests.post(DOUYIN_CONFIG["token_url"], json=payload, timeout=10)
        res.raise_for_status()
        resp_json = res.json()
        self._check_error(resp_json, "获取token")
        data = resp_json.get("data", {})
        self.save_token_to_db(data)
        return data.get("advertiser_ids", [])

    def get_advertiser_ids(self) -> list:
        """获取当前token已授权的广告主账户列表（含 advertiser_id / advertiser_name / account_role）"""
        access_token = self.get_access_token()
        resp = requests.get(
            DOUYIN_CONFIG["advertiser_get_url"],
            params={"access_token": access_token},
            headers={"Access-Token": access_token},
            timeout=10,
        )
        resp_json = resp.json()
        self._check_error(resp_json, "获取广告主列表")
        return resp_json.get("data", {}).get("list", [])

    def refresh_access_token(self):
        """使用refresh_token刷新access_token（巨量引擎 OAuth2.0，POST + JSON）"""
        self._load_from_db()
        if not self.token_info:
            raise Exception("无refresh_token，请先使用auth_code授权")
        payload = {
            "app_id": DOUYIN_CONFIG["APP_ID"],
            "secret": DOUYIN_CONFIG["Secret"],
            "grant_type": "refresh_token",
            "refresh_token": self.token_info.refresh_token,
        }
        resp = requests.post(DOUYIN_CONFIG["refresh_token_url"], json=payload, timeout=10)
        resp_json = resp.json()
        self._check_error(resp_json, "刷新token")
        self.save_token_to_db(resp_json.get("data", {}))


# 注意：这里不要直接实例化！删掉 token_mgr = DouYinTokenManager()
token_mgr: DouYinTokenManager | None = None


def get_token_mgr() -> DouYinTokenManager:
    global token_mgr
    if token_mgr is None:
        # 确保表已创建，避免因导入顺序在 init_db() 之前查询数据库
        init_db()
        token_mgr = DouYinTokenManager()
    return token_mgr
