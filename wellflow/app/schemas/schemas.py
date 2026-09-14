"""
WellFlow 通用响应 Schema
"""

from pydantic import BaseModel


class ApiResponse(BaseModel):
    code: int = 0
    data: object = None
    message: str = "ok"


class PageMeta(BaseModel):
    total: int
    page: int
    page_size: int


class PageResponse(BaseModel):
    items: list
    total: int
    page: int
    page_size: int
