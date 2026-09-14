"""WellFlow 统一错误模型

三类错误分类（final.md 第 9 节）：
- recoverable_business: 可恢复业务错误（无输入、字段冲突、未选方案等）
- transient_external: 外部服务暂时性错误（429、5xx、超时、搜索暂不可用）
- unrecoverable: 不可恢复异常（schema 不兼容、DB 约束、程序 bug、权限）
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    RECOVERABLE_BUSINESS = "recoverable_business"
    TRANSIENT_EXTERNAL = "transient_external"
    UNRECOVERABLE = "unrecoverable"


class TaskError(Exception):
    """结构化任务错误。业务层只抛出这个类型，节点内部的原始异常必须先归类。"""

    def __init__(
        self,
        code: str,
        category: ErrorCategory,
        message: str,
        source: str = "",
        retryable: bool | None = None,
        event_id: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        self.code = code
        self.category = category
        self.message = message
        self.source = source
        self.retryable = (
            retryable
            if retryable is not None
            else category == ErrorCategory.TRANSIENT_EXTERNAL
        )
        self.event_id = event_id
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category.value,
            "retryable": self.retryable,
            "source": self.source,
            "message": self.message,
            "event_id": self.event_id,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# 常用错误工厂
# ---------------------------------------------------------------------------


def business_error(code: str, message: str, source: str = "") -> TaskError:
    return TaskError(code, ErrorCategory.RECOVERABLE_BUSINESS, message, source=source)


def transient_error(code: str, message: str, source: str = "") -> TaskError:
    return TaskError(code, ErrorCategory.TRANSIENT_EXTERNAL, message, source=source)


def fatal_error(code: str, message: str, source: str = "") -> TaskError:
    return TaskError(code, ErrorCategory.UNRECOVERABLE, message, source=source)


# ---------------------------------------------------------------------------
# 预定义错误码
# ---------------------------------------------------------------------------

INPUT_REQUIRED = ("INPUT_REQUIRED", "至少提供商品图片或文字描述")
PRODUCT_FIELD_CONFLICT = ("PRODUCT_FIELD_CONFLICT", "图片识别结果与用户提供规格冲突，请 C1 处理")
NO_SELECTED_PLAN = ("NO_SELECTED_PLAN", "C2 未选择任何方案")
NO_AVAILABLE_ASSET = ("NO_AVAILABLE_ASSET", "资产库无符合条件的候选，请补充资产或修改筛选条件")
MODEL_NOT_WHITELISTED = ("MODEL_NOT_WHITELISTED", "指定的 image_model 不在白名单内")

LLM_TIMEOUT = ("LLM_TIMEOUT", "LLM 调用超时")
LLM_RATE_LIMIT = ("LLM_RATE_LIMIT", "LLM 触发 429 限流")
SEARCH_UNAVAILABLE = ("SEARCH_UNAVAILABLE", "外部搜索服务暂不可用")

SCHEMA_INCOMPATIBLE = ("SCHEMA_INCOMPATIBLE", "结构化输出 schema 不兼容")
DB_CONSTRAINT = ("DB_CONSTRAINT", "数据库约束错误")
CONFIG_ERROR = ("CONFIG_ERROR", "配置错误或权限缺失")
