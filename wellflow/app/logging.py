"""Wellflow logs: readable console output and optional structured JSON.

ContextVars carry page/operation context through async tasks and to_thread;
this module does not change root logging or any other subsystem's output.
"""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import inspect
import json
import os
import logging as std_logging
import re
import traceback

context: ContextVar[dict] = ContextVar("wellflow_log_context", default={})
call_context: ContextVar[dict | None] = ContextVar("wellflow_log_call", default=None)


def build_record(status: str, *, page: str | None = None, business: str | None = None, **fields) -> dict:
    record = {**context.get(), **(call_context.get() or {}), **fields}
    record["page"] = record.get("page") or page or "系统"
    record["biz"] = business or record.pop("business", None) or "运行状态"
    record.pop("business", None)
    if record["biz"] == "newapi网关调用":
        record["biz"] = "newapi网关"
    aliases = {"conversation_id": "conv_id", "sku_id": "sku", "endpoint": "api"}
    for old, new in aliases.items():
        value = record.pop(old, None)
        if value is not None:
            record.setdefault(new, value)
    record["action"] = "开始调用" if status == "开始" and (call_context.get() or record["biz"] == "newapi网关") else status
    record["ts"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
    record = {key: value for key, value in record.items() if value is not None and value != ""}
    if record.get("trace_id"):
        record.setdefault("parent_trace_id", None)
    order = ("ts", "biz", "trace_id", "parent_trace_id", "conv_id", "task_id", "sku", "model", "api", "action")
    ordered = {key: record.pop(key) for key in order if key in record}
    ordered.update(record)
    return ordered


def _level(action: str) -> str:
    if any(word in action for word in ("失败", "中断")):
        return "ERROR"
    if any(word in action for word in ("警告", "重试", "取消", "无效", "未知")):
        return "WARN"
    return "INFO"


def render_record(record: dict, *, output_format: str = "console") -> str:
    if output_format == "json":
        return json.dumps(record, ensure_ascii=False, default=str)
    if output_format != "console":
        raise ValueError("WELLFLOW_LOG_FORMAT must be console or json")
    def single(value):
        return str(value).replace("\r", "\\r").replace("\n", "\\n")
    timestamp = datetime.fromisoformat(record["ts"]).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"[{timestamp}] [{_level(record['action'])}] [{single(record['biz'])}]"
    if record.get("trace_id"):
        line += f" [trace:{single(record['trace_id'])}]"
    line += " " + single(record["action"])
    if record.get("api"):
        line += " " + single(record["api"])
    if record.get("message"):
        line += " " + single(record["message"])
    labels = {"task_id": "task", "elapsed_s": "elapsed_s"}
    details = []
    for key in ("conv_id", "task_id", "sku", "model", "elapsed_s", "page"):
        if key not in record or record[key] is None:
            continue
        value = record[key]
        details.append(f"{labels.get(key, key)}={single(value)}")
    shown = {"ts", "biz", "trace_id", "action", "api", "message", "conv_id", "task_id", "sku", "model", "elapsed_s", "page", "parent_trace_id"}
    details.extend(f"{key}={single(value)}" for key, value in record.items() if key not in shown and value is not None)
    return line + (" | " + " | ".join(details) if details else "")


def format_event(status: str, *, page: str | None = None, business: str | None = None, output_format: str | None = None, **fields) -> str:
    record = build_record(status, page=page, business=business, **fields)
    return render_record(record, output_format=output_format or os.getenv("WELLFLOW_LOG_FORMAT", "console"))


def log_event(status: str, *, page: str | None = None, business: str | None = None, **fields) -> None:
    print(format_event(status, page=page, business=business, **fields), flush=True)


class MigrationFormatter(std_logging.Formatter):
    """Used only by wellflow/alembic.ini, never installed on the application root logger."""
    def format(self, record: std_logging.LogRecord) -> str:
        fields = {"message": record.getMessage(), "logger": record.name}
        if record.exc_info:
            fields["traceback"] = self.formatException(record.exc_info)
        status = "失败" if record.levelno >= std_logging.ERROR else "警告" if record.levelno >= std_logging.WARNING else "记录"
        return format_event(status, page="系统", business="数据库迁移", **fields)


def log_message(*parts, page: str, business: str, status: str = "记录", sep: str = " ", exc_info: bool = False) -> None:
    """Keep useful diagnostic details inside JSON rather than mixed text lines."""
    message = sep.join(str(part) for part in parts).strip()
    message = re.sub(r"^(?:[^\w\[【]*\[[^\]]*\]\s*)+", "", message).strip()
    fields = {"message": message}
    if exc_info:
        fields["traceback"] = traceback.format_exc().rstrip()
    log_event(status, page=page, business=business, **fields)


def page_context(page: str):
    """Bind a page at application entry points without changing their signatures."""
    def decorate(fn):
        signature = inspect.signature(fn)
        def bind(args, kwargs):
            values = signature.bind_partial(*args, **kwargs).arguments
            extra = {key: values[key] for key in ("task_id", "conversation_id", "sku_id")
                     if values.get(key) is not None}
            return context.set({**context.get(), **extra, "page": page})

        if inspect.isasyncgenfunction(fn):
            @wraps(fn)
            async def wrapped(*args, **kwargs):
                iterator = fn(*args, **kwargs)
                try:
                    while True:
                        token = bind(args, kwargs)
                        try:
                            item = await anext(iterator)
                        except StopAsyncIteration:
                            return
                        finally:
                            context.reset(token)
                        yield item
                finally:
                    token = bind(args, kwargs)
                    try:
                        await iterator.aclose()
                    finally:
                        context.reset(token)
        elif inspect.iscoroutinefunction(fn):
            @wraps(fn)
            async def wrapped(*args, **kwargs):
                token = bind(args, kwargs)
                try:
                    return await fn(*args, **kwargs)
                finally:
                    context.reset(token)
        else:
            @wraps(fn)
            def wrapped(*args, **kwargs):
                token = bind(args, kwargs)
                try:
                    return fn(*args, **kwargs)
                finally:
                    context.reset(token)
        annotations = inspect.get_annotations(fn, eval_str=True)
        wrapped.__signature__ = signature.replace(
            parameters=[p.replace(annotation=annotations.get(p.name, p.annotation)) for p in signature.parameters.values()],
            return_annotation=annotations.get("return", signature.return_annotation),
        )
        return wrapped
    return decorate
