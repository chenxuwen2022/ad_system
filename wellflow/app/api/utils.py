"""统一响应格式：code / data / message。

路由层手动调 ok(data) 返回成功，HTTPException 由 main.py 的异常处理器统一转为失败格式。
OpenAPI 文档用 response_model=StandardResponse[T] 声明，Swagger 与实际返回完全对齐。

格式约定：
  - 成功：code=0, data=业务数据, message="ok"
  - 失败：code=HTTP 状态码或业务码, data=错误详情/None, message=错误描述
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

try:
    # Python 3.9+ 标准库
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

_CN_TZ = ZoneInfo("Asia/Shanghai")

T = TypeVar("T")


def to_cn_iso(dt: datetime | None) -> str | None:
    """把数据库里的 UTC datetime（aware 或 naive 都兼容）转成北京时间 ISO 字符串。

    后端统一用 UTC 存，但前端展示按北京时间。直接转成带 +08:00 的 ISO 后，
    前端 JS 的 new Date(iso) 就能按用户浏览器时区正确解析。
    """
    if dt is None:
        return None
    # SQLAlchemy 从 DateTime(timezone=True) 读出的在有些驱动下是 naive（但值是 UTC）
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_CN_TZ).isoformat()


class StandardResponse(BaseModel, Generic[T]):
    """统一响应包装。路由声明 response_model=StandardResponse[SkuDetailResponse]。"""
    code: int = 0
    data: T | None = None
    message: str = "ok"


def ok(data: Any = None, message: str = "ok", code: int = 0) -> dict:
    """成功响应（返回 dict，FastAPI 按 response_model 序列化）。"""
    return {"code": code, "data": data, "message": message}


def fail(message: str = "error", code: int = 1, data: Any = None) -> dict:
    """失败响应（主要用于全局异常处理器，路由层一般 raise HTTPException）。"""
    return {"code": code, "data": data, "message": message}


def page(items: list, total: int, page_num: int, page_size: int) -> dict:
    """分页响应。"""
    return {
        "code": 0,
        "data": {
            "items": items,
            "total": total,
            "page": page_num,
            "page_size": page_size,
        },
        "message": "ok",
    }
