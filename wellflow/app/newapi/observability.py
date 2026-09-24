"""Correlated business and model lifecycle logs; never log prompts or image payloads."""
from __future__ import annotations

import asyncio
from functools import wraps
import inspect
import time
from uuid import uuid4

from wellflow.app.logging import context as _context, call_context as _call, log_event


def event(status: str, **fields) -> None:
    if _call.get():
        fields.setdefault("business", "newapi网关调用")
    log_event(status, **fields)


def request_prepared(model: str, endpoint: str, attempt: int = 1) -> None:
    call = _call.get()
    if call is not None:
        call.pop("request_id", None)
        call.pop("http_status", None)
        call.update(request_model=model, endpoint=endpoint, attempt=attempt)


def response_received(response, model: str, endpoint: str) -> None:
    call = _call.get()
    if call is not None:
        call.update(request_id=response.headers.get("x-oneapi-request-id"),
                    http_status=response.status_code, request_model=model, endpoint=endpoint)


def _failure(exc: BaseException, started: float) -> None:
    # Exception messages may contain request bodies; retain a bounded summary only.
    response = getattr(exc, "response", None)
    event("取消" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else "失败",
          elapsed_s=round(time.monotonic() - started, 3), error_type=type(exc).__name__,
          error=str(getattr(exc, "detail", exc)).replace("\n", " ")[:400],
          **({"http_status": response.status_code} if response is not None else {}))


def business_operation(name: str, *, lifecycle: bool = True):
    """Bind per-invocation context, including concurrent child image jobs."""
    def decorate(fn):
        signature = inspect.signature(fn)
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            bound = signature.bind_partial(*args, **kwargs).arguments
            state = bound.get("state") or bound.get("task") or {}
            parent = _context.get()
            labels = {
                "Node1/商品分析": ("对话", "node1产品报告"),
                "Node2/商拍方案": ("对话", "node2商拍方案"),
                "Node3/提示词生成": ("对话", "node3生图提示词"),
                "Node4/生图": ("对话", "node4图片生成"),
                "Node1/报告微调": ("对话", "node1产品报告微调"),
                "Node2/方案微调": ("对话", "node2商拍方案微调"),
                "Node3/提示词微调": ("对话", "node3提示词微调"),
                "意图识别": ("对话", "意图识别"),
            }
            page, business = labels.get(name, (None, name))
            if "/" in name and page is None:
                page, business = name.split("/", 1)
            if name == "生图":
                business = "大模型调用"
            context = {**parent, "page": parent.get("page") or page or "系统",
                       "business": business, "trace_id": uuid4().hex[:12]}
            task_id = bound.get("task_id") or state.get("task_id") or state.get("id")
            if task_id:
                context["task_id"] = task_id
            if parent.get("trace_id"):
                context["parent_trace_id"] = parent["trace_id"]
            if bound.get("log_id"):
                context["image_job"] = bound["log_id"]
            if bound.get("name"):
                context["item"] = bound["name"]
            token = _context.set(context)
            started = time.monotonic()
            if lifecycle:
                event("业务开始")
            try:
                result = await fn(*args, **kwargs)
                if lifecycle:
                    event("业务完成", elapsed_s=round(time.monotonic() - started, 3))
                return result
            except BaseException as exc:
                _failure(exc, started)
                raise
            finally:
                _context.reset(token)
        # FastAPI resolves forward annotations against the wrapper's globals.
        annotations = inspect.get_annotations(fn, eval_str=True)
        wrapped.__signature__ = signature.replace(
            parameters=[p.replace(annotation=annotations.get(p.name, p.annotation))
                        for p in signature.parameters.values()],
            return_annotation=annotations.get("return", signature.return_annotation),
        )
        return wrapped
    return decorate


def model_call(endpoint: str):
    def decorate(fn):
        def begin(self):
            return {"trace_id": uuid4().hex[:12], "parent_trace_id": _context.get().get("trace_id"),
                    "model": getattr(self, "model", ""), "endpoint": endpoint}
        if inspect.isasyncgenfunction(fn):
            @wraps(fn)
            async def stream(self, *args, **kwargs):
                call = begin(self)
                started = time.monotonic()
                chunks = 0
                iterator = fn(self, *args, **kwargs)
                try:
                    while True:
                        token = _call.set(call)
                        try:
                            if chunks == 0:
                                event("开始", stream=True)
                            item = await anext(iterator)
                        except StopAsyncIteration:
                            event("调用完成", elapsed_s=round(time.monotonic()-started, 3), chunks=chunks)
                            return
                        except BaseException as exc:
                            _failure(exc, started)
                            raise
                        finally:
                            _call.reset(token)
                        chunks += 1
                        try:
                            yield item
                        except BaseException as exc:
                            token = _call.set(call)
                            try:
                                _failure(exc, started)
                            finally:
                                _call.reset(token)
                            raise
                finally:
                    await iterator.aclose()
            return stream
        @wraps(fn)
        async def wrapped(self, *args, **kwargs):
            token = _call.set(begin(self))
            started = time.monotonic()
            event("开始")
            try:
                result = await fn(self, *args, **kwargs)
                event("调用完成", elapsed_s=round(time.monotonic()-started, 3),
                      response_model=getattr(result, "model", ""))
                return result
            except BaseException as exc:
                _failure(exc, started)
                raise
            finally:
                _call.reset(token)
        return wrapped
    return decorate
