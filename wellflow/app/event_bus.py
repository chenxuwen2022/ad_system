"""进程内事件总线 — graph 运行时 publish，SSE 端点 subscribe。

设计：
  - 全局 dict: {task_id: asyncio.Queue}，每个任务一个队列
  - publish(task_id, event_type, data) 往队列里扔事件
  - subscribe(task_id) 返回 Queue；等待与心跳由 SSE 层负责
  - queue 满了自动淘汰最旧的（防内存泄漏）
  - 无外部依赖（不需要 Redis/LISTEN NOTIFY）

为何不用 Redis / LISTEN NOTIFY？
  - 当前 graph 运行和 SSE 订阅在同一个 uvicorn 进程里，进程内 Queue 最简单
  - 如果以后拆到多进程 / 多台机器，再升级为 Redis pub/sub
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from wellflow.app.config import settings

# task_id -> asyncio.Queue
_queues: dict[str, asyncio.Queue] = {}
# 队列容量 / 闲置清理超时统一在 config.py（event_bus_queue_max_size / event_bus_queue_idle_timeout_seconds）

# ---------------------------------------------------------------------------
# graph 运行状态追踪 —— 防止同一个 task_id 并发启动多个 graph 执行
# ---------------------------------------------------------------------------

# 正在执行 graph 的 task_id 集合（graph 运行时 = 不在 interrupt 等待）
_running_tasks: set[str] = set()


def mark_running(task_id: str) -> None:
    """标记 task 正在执行 graph。"""
    _running_tasks.add(task_id)


def mark_done(task_id: str) -> None:
    """标记 task 的 graph 已结束（正常完成、interrupt 暂停、或报错）。"""
    _running_tasks.discard(task_id)


def is_running(task_id: str) -> bool:
    """检查 task 是否正在执行 graph。"""
    return task_id in _running_tasks


def _get_or_create_queue(task_id: str) -> asyncio.Queue:
    """获取或创建队列；订阅操作不消费已有事件。"""
    q = _queues.get(task_id)
    if q is None:
        q = asyncio.Queue(maxsize=settings.event_bus_queue_max_size)
        _queues[task_id] = q
    return q


def publish(task_id: str, event_type: str, data: dict[str, Any]) -> None:
    """往指定任务的队列里扔一个事件。非阻塞，安全。

    调用方（graph 运行线程）不需要 await，也不关心有没有订阅者。
    """
    q = _get_or_create_queue(task_id)
    try:
        q.put_nowait({
            "type": event_type,
            "data": data,
            "ts": time.time(),
        })
    except asyncio.QueueFull:
        # 队列已满时，仅发布新事件才淘汰最旧事件
        try:
            q.get_nowait()
            q.put_nowait({"type": event_type, "data": data, "ts": time.time()})
        except Exception:
            pass  # 实在塞不进去就丢了，轮询 fallback 能补


async def subscribe(task_id: str) -> "asyncio.Queue[dict[str, Any]]":
    """订阅指定任务的事件。返回一个 Queue，订阅者 get() 即可。

    Args:
        task_id: 任务 ID

    Returns:
        asyncio.Queue — 订阅者 await queue.get() 拿事件
    """
    # 订阅者用一个专门的队列，publish 时往所有订阅者队列推
    # 简化实现：直接用 _queues[task_id]，因为同一个 task_id 只会有一个 SSE 订阅者
    q = _get_or_create_queue(task_id)
    return q


async def drain_and_subscribe(task_id: str) -> "asyncio.Queue[dict[str, Any]]":
    """清空旧积压事件，然后返回新订阅队列。

    前端重连 SSE 时调用，避免收到上一轮连接的历史事件。
    """
    q = _queues.get(task_id)
    if q is not None:
        while not q.empty():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
    return _get_or_create_queue(task_id)


def cleanup(task_id: str) -> None:
    """任务结束后清理队列。"""
    _queues.pop(task_id, None)


# 清理孤儿队列（定时调用，可选）
async def _gc_idle_queues() -> None:
    """后台定时清理闲置队列。"""
    while True:
        await asyncio.sleep(settings.event_bus_queue_idle_timeout_seconds)
        # 遍历清理（注意不能在迭代 dict 时删，所以先 collect）
        stale = [tid for tid in _queues]
        for tid in stale:
            # 简单策略：直接清——如果任务还活着，下次 publish 会重建
            if tid in _queues:
                _queues.pop(tid, None)
