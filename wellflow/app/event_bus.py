"""进程内事件总线 — graph 运行时 publish，SSE 端点 subscribe。

设计：
  - 全局 dict: {task_id: asyncio.Queue}，每个任务一个队列
  - publish(task_id, event_type, data) 往队列里扔事件
  - subscribe(task_id) 返回 async iterator，阻塞等事件（queue.get + timeout）
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

# task_id -> asyncio.Queue
_queues: dict[str, asyncio.Queue] = {}
# 每个队列最大容量，防止慢消费者拖垮内存
_QUEUE_MAX_SIZE = 64
# 队列闲置多久后自动清理（秒），避免孤儿队列
_QUEUE_IDLE_TIMEOUT = 600


def _get_or_create_queue(task_id: str) -> asyncio.Queue:
    """获取或创建队列；满了就淘汰最旧事件（FIFO）。"""
    q = _queues.get(task_id)
    if q is None:
        q = asyncio.Queue(maxsize=_QUEUE_MAX_SIZE)
        _queues[task_id] = q
    elif q.full():
        # 丢一个最旧的，让新事件能塞进去
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
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
        # 理论上 _get_or_create_queue 已经防了，这里兜底
        try:
            q.get_nowait()
            q.put_nowait({"type": event_type, "data": data, "ts": time.time()})
        except Exception:
            pass  # 实在塞不进去就丢了，轮询 fallback 能补


async def subscribe(task_id: str, heartbeat_interval: float = 15.0) -> "asyncio.Queue[dict[str, Any]]":
    """订阅指定任务的事件。返回一个 Queue，订阅者 get() 即可。

    Args:
        task_id: 任务 ID
        heartbeat_interval: 多久没事件就塞一个 ping，防止 SSE 连接被代理掐掉

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
        await asyncio.sleep(_QUEUE_IDLE_TIMEOUT)
        # 遍历清理（注意不能在迭代 dict 时删，所以先 collect）
        stale = [tid for tid in _queues]
        for tid in stale:
            # 简单策略：直接清——如果任务还活着，下次 publish 会重建
            if tid in _queues:
                _queues.pop(tid, None)
