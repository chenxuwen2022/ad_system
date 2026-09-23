"""生图共享入口：限制进程内并发、错峰发送，仅重试明确拒绝的 429。

超时/断连可能已经产生图片和费用，不在这里自动重发。多进程部署需要在
网关侧同时配置共享配额；这里的限制只覆盖当前进程。
"""
from __future__ import annotations

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

    async def run():
        for attempt in range(settings.image_rate_limit_retries + 1):
            print(f"[生图请求 {log_id}] 等待共享并发额度/发送间隔/限流冷却 "
                  f"attempt={attempt + 1}/{settings.image_rate_limit_retries + 1}", flush=True)
            async with gate.slots:
                await gate.wait()
                print(f"[生图请求 {log_id}] 已获得发送额度，调用上游 "
                      f"attempt={attempt + 1}/{settings.image_rate_limit_retries + 1}", flush=True)
                try:
                    return await client.generate_image(**kwargs)
                except ImageRateLimitError as exc:
                    delay = retry_delay(exc.retry_after, attempt)
                    gate.cooldown_until = max(
                        gate.cooldown_until, asyncio.get_running_loop().time() + delay
                    )
                    if attempt >= settings.image_rate_limit_retries:
                        raise
                    text = (f"{log_id} 遇到限流，冷却至少 {delay:.1f} 秒后重新竞争发送额度"
                            f"（{attempt + 1}/{settings.image_rate_limit_retries}），已完成图片保留。")
                    print(f"[{log_id}] ⏳ {text}", flush=True)
                    if task_id:
                        from wellflow.app.event_bus import publish
                        publish(task_id, "message", {"text": text})
            # 下一次尝试也经过同一个限流入口，其他任务同时遵守 cooldown。

    try:
        return await asyncio.wait_for(run(), timeout=settings.image_retry_deadline)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            f"{log_id} 生图排队或重试超过 {settings.image_retry_deadline:g} 秒，已停止本模型尝试"
        ) from exc
