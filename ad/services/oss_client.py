# -*- coding: utf-8 -*-
"""阿里云 OSS 客户端：素材双写（本地=投放用，OSS=展示用）。

设计要点：
- 本地磁盘文件必须保留：千川投放接口只收文件流，依赖 local_file_path。
- OSS 仅用于页面展示加速（缩略图/大图直连 OSS，不占后端带宽）。
- Bucket 当前为私有读写，因此前端 URL 用"临时签名 URL"（默认 6 小时有效）。
  后续若配 CDN / 公共读，改 OSS_PUBLIC_BASE 环境变量即可切换为固定域名直链。
"""
import os
import time
import threading
import logging

logger = logging.getLogger(__name__)

try:
    import oss2
except ImportError:  # pragma: no cover
    oss2 = None  # 未安装 oss2 时降级为纯本地模式，不影响投放

# ---- 配置（从环境变量 / ad/.env 读取）----
ACCESS_KEY_ID = os.environ.get("OSS_ACCESS_KEY_ID", "")
ACCESS_KEY_SECRET = os.environ.get("OSS_ACCESS_KEY_SECRET", "")
BUCKET = os.environ.get("OSS_BUCKET", "wellflow-saas-launch")
# 内网 endpoint（ECS 同区部署时走内网，免公网流量）；本地开发走外网
_ENDPOINT_PUBLIC = os.environ.get("OSS_ENDPOINT", "oss-cn-beijing.aliyuncs.com")
_IS_INTERNAL = os.environ.get("OSS_INTERNAL", "1") == "1"
ENDPOINT = (_ENDPOINT_PUBLIC.replace("oss-cn-beijing", "oss-cn-beijing-internal")
            if _IS_INTERNAL else _ENDPOINT_PUBLIC)
# 若配置了 CDN/公共域名，则签名 URL 替换为该域名直链（公共读场景）
PUBLIC_BASE = os.environ.get("OSS_PUBLIC_BASE", "").rstrip("/")

_bucket = None
_lock = threading.Lock()

# OSS 上的 key 前缀
KEY_MEDIA = "media/"   # 原图/原视频
KEY_THUMB = "thumb/"  # 300px 缩略图（jpg）


def _ensure_bucket():
    """懒加载 bucket 单例。未配置 AK 或未装 oss2 时返回 None（降级模式）。"""
    global _bucket
    if _bucket is not None:
        return _bucket
    if not ACCESS_KEY_ID or not ACCESS_KEY_SECRET or not BUCKET:
        return None
    if oss2 is None:
        return None
    with _lock:
        if _bucket is None:
            auth = oss2.Auth(ACCESS_KEY_ID, ACCESS_KEY_SECRET)
            _bucket = oss2.Bucket(auth, f"https://{ENDPOINT}", BUCKET)
    return _bucket


def enabled() -> bool:
    """OSS 是否可用（未配置时整体降级，不影响任何本地逻辑）。"""
    return _ensure_bucket() is not None


def media_key(fname: str) -> str:
    fname = os.path.basename(fname).replace("\\", "/")
    return KEY_MEDIA + fname


def thumb_key(fname: str) -> str:
    """缩略图在 OSS 上的 key：thumb/<去扩展名>.jpg（与本地 media_storage/thumb 命名一致）。"""
    stem = os.path.splitext(os.path.basename(fname))[0]
    return KEY_THUMB + stem + ".jpg"


def upload_file(local_path: str, key: str) -> bool:
    """上传本地文件到 OSS。失败仅记日志，不阻塞主流程（本地文件仍在）。"""
    b = _ensure_bucket()
    if b is None:
        return False
    if not os.path.isfile(local_path):
        return False
    try:
        b.put_object_from_file(key, local_path)
        logger.info(f"[OSS] uploaded {key} ({os.path.getsize(local_path)} bytes)")
        return True
    except Exception as e:
        logger.warning(f"[OSS] upload failed {key}: {e}")
        return False


def object_exists(key: str) -> bool:
    b = _ensure_bucket()
    if b is None:
        return False
    try:
        return b.object_exists(key)
    except Exception:
        return False


def sign_url(key: str, expires: int = 6 * 3600) -> str:
    """生成临时签名 GET URL（私有 bucket）。PUBLIC_BASE 配置后直接返回公共直链。"""
    if PUBLIC_BASE:
        return f"{PUBLIC_BASE}/{key}"
    b = _ensure_bucket()
    if b is None:
        return ""
    try:
        return b.sign_url("GET", key, expires=expires)
    except Exception:
        return ""


_thumb_cache = None
_thumb_cache_ts = 0.0
_THUMB_CACHE_TTL = 5 * 60


def _load_thumb_index():
    global _thumb_cache, _thumb_cache_ts
    b = _ensure_bucket()
    if b is None:
        _thumb_cache = set()
        return
    try:
        keys = set()
        for obj in oss2.ObjectIterator(b, prefix=KEY_THUMB, max_keys=1000):
            keys.add(obj.key)
        _thumb_cache = keys
        _thumb_cache_ts = time.time()
    except Exception as e:
        logger.warning(f"[OSS] list thumb failed: {e}")
        if _thumb_cache is None:
            _thumb_cache = set()


def has_thumb(fname: str) -> bool:
    if not enabled():
        return False
    if _thumb_cache is None or (time.time() - _thumb_cache_ts) > _THUMB_CACHE_TTL:
        _load_thumb_index()
    return thumb_key(fname) in _thumb_cache


def invalidate_thumb_cache():
    global _thumb_cache, _thumb_cache_ts
    _thumb_cache = None
    _thumb_cache_ts = 0


def get_thumb_url(fname: str) -> str:
    """OSS 缩略图签名 URL；无则空串（调用方回退本地 /api/thumb/）。"""
    if not enabled():
        return ""
    key = thumb_key(fname)
    if has_thumb(fname):
        return sign_url(key)
    return ""
