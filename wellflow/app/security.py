"""JWT authentication. The database is the authority for current role/company."""
import hashlib
import secrets
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from wellflow.app.config import settings
from wellflow.app.database import SessionLocal, get_db
from wellflow.app.models.account_models import User

PLATFORM_ADMIN = "platform_admin"
ADMIN_ROLES = {PLATFORM_ADMIN, "company_admin"}


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if not raw or len(raw) > 72:
        raise ValueError("密码不能为空，且不能超过 72 个 UTF-8 字节")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, UnicodeError):
        return False


@lru_cache(maxsize=1)
def dummy_password_hash():
    return hash_password(secrets.token_urlsafe(24))


def signing_key():
    key = settings.auth_jwt_secret.get_secret_value()
    if len(key.encode("utf-8")) < 32:
        raise HTTPException(503, "登录服务未配置，请联系管理员设置 AUTH_JWT_SECRET")
    return key


def issue_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({
        "sub": str(user.id), "company_id": user.company_id, "role": user.role,
        "iat": now, "exp": now + timedelta(hours=settings.auth_token_hours),
        "iss": "wellflow", "aud": "wellflow-api",
    }, signing_key(), algorithm="HS256")


def decode_token(token: str):
    try:
        payload = jwt.decode(token, signing_key(), algorithms=["HS256"],
                             issuer="wellflow", audience="wellflow-api",
                             options={"require": ["sub", "company_id", "role", "iat", "exp", "iss", "aud"]})
        if not isinstance(payload["sub"], str) or not payload["sub"].isdigit():
            raise ValueError("invalid subject")
        return payload
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise HTTPException(401, "登录已失效，请重新登录", headers={"WWW-Authenticate": "Bearer"}) from exc


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    cached = getattr(request.state, "account_user", None)
    if cached is not None:
        # The global authentication session is already closed. Load into the
        # endpoint's own session so management mutations operate on live rows.
        user = db.get(User, cached.id)
        if user is None or user.deleted_at is not None:
            raise HTTPException(401, "账号已删除，请联系管理员")
        return user
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    # Images rendered by <img> / <video> / <audio> tags cannot attach an
    # Authorization header. For these read-only media endpoints we fall back
    # first to a ?token= query param (most reliable across proxy/domain setups)
    # and second to the wellflow_media HttpOnly cookie set at login.
    # NOTE: /uploads and /static are mounted as FastAPI StaticFiles outside
    # the app-level dependencies=[] scope so they need no handling here.
    if request.method in {"GET", "HEAD"} and not token:
        media_path = request.url.path
        media_endpoint = (
            media_path.startswith("/api/uploaded_media/")
            or media_path.startswith("/api/wellflow/image/thumbnail")
            or media_path.startswith("/api/file_by_path")
        )
        if media_endpoint:
            token = request.query_params.get("token", "")
            scheme = "Bearer"
            if not token:
                token = request.cookies.get("wellflow_media", "")
                scheme = "Bearer"
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "请先登录", headers={"WWW-Authenticate": "Bearer"})
    payload = decode_token(token)
    user = db.get(User, int(payload["sub"]))
    if user is None or user.deleted_at is not None:
        raise HTTPException(401, "账号已删除，请联系管理员")
    if payload["company_id"] != user.company_id:
        raise HTTPException(401, "所属公司已变更，请重新登录", headers={"WWW-Authenticate": "Bearer"})
    request.state.account_user = user
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role not in ADMIN_ROLES:
        raise HTTPException(403, "仅管理员可执行此操作")
    return user


def require_platform_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != PLATFORM_ADMIN:
        raise HTTPException(403, "仅平台超管可管理公司")
    return user


def get_auth_session_factory():
    return SessionLocal


# 投放台（wellflow-launch-frontend）接口免认证前缀：内部系统前端不携带 token，与旧版行为一致；
# wellflow 商拍子系统其余 /api/ 接口（/api/auth、/api/wellflow 等）仍走认证。
PUBLIC_API_PREFIXES = (
    "/api/ad", "/api/advertiser_accounts", "/api/categories", "/api/db_materials",
    "/api/launch_records", "/api/material_biz_statuses", "/api/material_detail_by_id",
    "/api/material_detail_by_name", "/api/material_stats", "/api/plan_products",
    "/api/plans", "/api/shops", "/api/sync_material_status", "/api/tags",
    "/api/thumb", "/api/upload_material", "/api/uploaded_media",
)


async def require_api_user(request: Request, session_factory=Depends(get_auth_session_factory)):
    """Release auth connection before streaming, retain immutable request scope."""
    from wellflow.app.ownership import Principal, principal
    if request.url.path.startswith(PUBLIC_API_PREFIXES):
        yield None
        return
    user = None
    scope_token = None
    if request.url.path.startswith("/api/") and request.url.path.rstrip("/") != "/api/auth/login":
        with session_factory() as db:
            user = get_current_user(request, db)
            scope_token = principal.set(Principal(user.id, user.company_id, user.role))
    try:
        yield user
    finally:
        if scope_token is not None:
            principal.reset(scope_token)


class LoginLimiter:
    """Bounded per-process limiter. Deploy an upstream shared limiter for multiple workers."""
    def __init__(self):
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def check(self, ip: str, username: str):
        now = time.monotonic()
        keys = [("ip:" + ip, 60), ("user:" + hashlib.sha256(username.encode()).hexdigest(), 10)]
        with self.lock:
            for key, limit in keys:
                attempts = self.entries.setdefault(key, deque())
                self.entries.move_to_end(key)
                while attempts and attempts[0] <= now - 60:
                    attempts.popleft()
                if len(attempts) >= limit:
                    raise HTTPException(429, "登录尝试过于频繁，请一分钟后重试", headers={"Retry-After": "60"})
            for key, _ in keys:
                self.entries[key].append(now)
            while len(self.entries) > 10000:
                self.entries.popitem(last=False)


login_limiter = LoginLimiter()
