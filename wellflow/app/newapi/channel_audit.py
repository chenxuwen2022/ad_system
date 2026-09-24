"""Report the channel actually recorded by New API for a model request."""

from __future__ import annotations

from wellflow.app.logging import log_message

import asyncio
import threading
from typing import Any

import httpx

from wellflow.app.config import settings
from wellflow.app.newapi.observability import event


_background_tasks: set[asyncio.Task[None]] = set()


async def log_actual_channel(request_id: str, model: str, operation: str) -> None:
    fields = dict(business="newapi渠道查询", endpoint=operation, request_model=model, request_id=request_id or None)
    if not request_id:
        event("渠道未知", **fields, reason="网关未返回请求ID")
        return
    if not settings.newapi_admin_access_token:
        return

    url = f"{settings.newapi_admin_base_url.rstrip('/')}/api/log/"
    headers = {"Authorization": f"Bearer {settings.newapi_admin_access_token}"}
    # New API writes usage logs after a request; allow a short interval for that write.
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
                result = await client.get(url, params={"request_id": request_id, "p": 1, "page_size": 10}, headers=headers)
                result.raise_for_status()
                body = result.json()
            if body.get("success") is not True:
                raise ValueError("日志接口返回失败")
            items = (body.get("data") or {}).get("items") or []
            match = next((item for item in items if item.get("request_id") == request_id), None)
            if match:
                channel_id = match.get("channel_id")
                channel_name = match.get("channel_name")
                if channel_id is not None:
                    event("渠道确认", **fields, channel_id=channel_id, channel_name=channel_name or None)
                    return
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            event("渠道查询失败", **fields, error_type=type(exc).__name__)
            return
        if attempt < 2:
            await asyncio.sleep(0.2 * (attempt + 1))
    event("渠道未知", **fields, reason="网关日志尚未写入或无匹配记录")


def _request_id(response: Any) -> str:
    return response.headers.get("x-oneapi-request-id", "")


def _consume_result(task: asyncio.Task[None]) -> None:
    _background_tasks.discard(task)
    try:
        task.result()
    except Exception as exc:  # audit failures must never reach the business request
        log_message(f"[llm-channel] 后台渠道查询异常: {type(exc).__name__}", page='系统', business='newapi渠道查询', status='记录')


def schedule_actual_channel(response: Any, model: str, operation: str) -> None:
    """Queue channel lookup on the current event loop and return immediately."""
    request_id = _request_id(response)
    if not settings.newapi_admin_access_token:
        return
    if not request_id:
        log_message(f"[llm-channel] operation={operation} model={model} request_id=未返回 "
            "actual_channel=无法获取 (网关未返回请求 ID)", page='系统', business='newapi渠道查询', status='记录')
        return
    task = asyncio.create_task(log_actual_channel(request_id, model, operation))
    _background_tasks.add(task)
    task.add_done_callback(_consume_result)


def schedule_actual_channel_sync(response: Any, model: str, operation: str) -> None:
    """Queue audit work in a daemon thread for legacy synchronous callers."""
    request_id = _request_id(response)
    if not settings.newapi_admin_access_token:
        return
    if not request_id:
        log_message(f"[llm-channel] operation={operation} model={model} request_id=未返回 "
            "actual_channel=无法获取 (网关未返回请求 ID)", page='系统', business='newapi渠道查询', status='记录')
        return

    def run() -> None:
        try:
            asyncio.run(log_actual_channel(request_id, model, operation))
        except Exception as exc:
            log_message(f"[llm-channel] 后台渠道查询异常: {type(exc).__name__}", page='系统', business='newapi渠道查询', status='记录')

    threading.Thread(target=run, name="llm-channel-audit", daemon=True).start()
