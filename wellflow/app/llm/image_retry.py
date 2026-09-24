"""生图共享入口：限制进程内并发、错峰发送，仅重试明确拒绝的 429。

超时/断连可能已经产生图片和费用，不在这里自动重发。多进程部署需要在
网关侧同时配置共享配额；这里的限制只覆盖当前进程。
"""
from __future__ import annotations

from wellflow.app.logging import log_event

import asyncio
import math
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from weakref import WeakKeyDictionary

from wellflow.app.config import settings
from wellflow.app.llm.base import ImageRateLimitError


class _Gate:
    def __init__(self):
        self.slots = asyncio.Semaphore(settings.image_request_concurrency)
        self.lock = asyncio.Lock()
        self.next_start = 0.0
        self.cooldown_until = 0.0

    async def wait(self):
        async with self.lock:
            loop = asyncio.get_running_loop()
            while True:
                delay = max(self.next_start, self.cooldown_until) - loop.time()
                if delay <= 0:
                    self.next_start = loop.time() + settings.image_request_interval
                    return
                await asyncio.sleep(delay)


_gates = WeakKeyDictionary()


def _gate():
    loop = asyncio.get_running_loop()
    if loop not in _gates:
        _gates[loop] = _Gate()
    return _gates[loop]


def retry_delay(value: str | None, attempt: int) -> float:
    """支持 Retry-After 秒数和 HTTP 日期，无效值使用退避+jitter。"""
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                delay = (when - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                delay = float('nan')
        if math.isfinite(delay) and delay >= 0:
            return delay
    return min(15.0 * 2 ** attempt, 60.0) + random.uniform(0, 3)


async def generate_with_rate_limit_retry(client, *, log_id, task_id=None, **kwargs):
    gate = _gate()
    loop = asyncio.get_running_loop()
    remaining_wait = settings.image_retry_deadline

    def record(action, **fields):
        log_event(action, business="生图请求", task_id=task_id, image_job=log_id,
                  model=getattr(client, "model", None), **fields)


    async def acquire():
        await gate.slots.acquire()
        try:
            await gate.wait()
        except BaseException:
            gate.slots.release()
            raise

    for attempt in range(settings.image_rate_limit_retries + 1):
        attempt_fields = {"attempt": attempt + 1,
                          "max_attempts": settings.image_rate_limit_retries + 1}
        record("等待发送额度", **attempt_fields,
               call_type="首次调用" if attempt == 0 else "限流后重试")
        wait_started = loop.time()
        try:
            await asyncio.wait_for(acquire(), timeout=remaining_wait)
        except asyncio.TimeoutError as exc:
            record("排队失败（超时）", **attempt_fields, retry=False,
                   reason="累计排队或限流等待超过时限，未发送本次请求")
            raise TimeoutError(
                f"{log_id} 累计排队/限流等待超过 {settings.image_retry_deadline:g} 秒，"
                "已停止本模型尝试（不含实际生图时间）"
            ) from exc
        remaining_wait = max(0.0, remaining_wait - (loop.time() - wait_started))
        try:
            record("开始调用" if attempt == 0 else "开始重试", **attempt_fields,
                   timeout_s=settings.image_timeout)
            started = loop.time()
            try:
                # 每次实际调用单独计时；429 后再次调用或切换模型均获得完整时限。
                result = await asyncio.wait_for(
                    client.generate_image(**kwargs), timeout=settings.image_timeout,
                )
                images = getattr(result, "all_images", [])
                record("调用成功", **attempt_fields, elapsed_s=round(loop.time() - started, 3),
                       response_model=getattr(result, "model", None),
                       image_count=len(images),
                       result_formats=sorted({kind for item in images
                                              for kind in ("url", "b64_json") if getattr(item, kind, None)}))
                return result
            except asyncio.TimeoutError as exc:
                record("调用失败（超时）", **attempt_fields, elapsed_s=round(loop.time() - started, 3),
                       retry=False, reason="上游可能仍在生成，避免重复生成和计费，不自动重试")
                raise TimeoutError(
                    f"{log_id} 本次模型生图调用超过 {settings.image_timeout:g} 秒，"
                    "已停止等待（不含排队/限流等待）"
                ) from exc
            except ImageRateLimitError as exc:
                delay = retry_delay(exc.retry_after, attempt)
                gate.cooldown_until = max(gate.cooldown_until, loop.time() + delay)
                will_retry = attempt < settings.image_rate_limit_retries
                record("限流，准备重试" if will_retry else "调用失败，重试次数已用尽",
                       **attempt_fields, elapsed_s=round(loop.time() - started, 3),
                       http_status=429, error_type=type(exc).__name__,
                       error=str(exc).replace("\n", " ")[:1000], retry=will_retry,
                       retry_after=exc.retry_after, retry_delay_s=round(delay, 3) if will_retry else None,
                       reason="上游明确返回 HTTP 429，允许重试" if will_retry else "已达到最大调用次数")
                if not will_retry:
                    raise
                text = (f"{log_id} 遇到限流，冷却至少 {delay:.1f} 秒后重新竞争发送额度"
                        f"（{attempt + 1}/{settings.image_rate_limit_retries}），已完成图片保留。")
                if task_id:
                    from wellflow.app.event_bus import publish
                    publish(task_id, "message", {"text": text})
            except Exception as exc:
                record("调用失败", **attempt_fields, elapsed_s=round(loop.time() - started, 3),
                       error_type=type(exc).__name__, error=str(exc).replace("\n", " ")[:1000],
                       retry=False, reason="非明确的 HTTP 429 限流错误，不自动重试")
                raise
        finally:
            gate.slots.release()
