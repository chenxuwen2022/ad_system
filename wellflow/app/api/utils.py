"""统一响应工具：确保所有接口返回 {code, data, message} 格式"""

from typing import Any, Optional

from fastapi.responses import JSONResponse


def ok(data: Any = None, message: str = "ok", code: int = 0) -> dict:
    """成功响应"""
    return {"code": code, "data": data, "message": message}


def fail(message: str = "error", code: int = 1, data: Any = None) -> dict:
    """失败响应"""
    return {"code": code, "data": data, "message": message}


def page(items: list, total: int, page_num: int, page_size: int) -> dict:
    """分页响应"""
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
