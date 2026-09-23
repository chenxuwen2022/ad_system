"""Node4 日志计数；每个任务每轮生成是一条队列，worker 是消费者。"""
from __future__ import annotations

import os
from uuid import uuid4


_active_queues: dict[str, "ImageQueueLog"] = {}


class ImageQueueLog:
    def __init__(self, task_id: str, total: int, workers: int):
        self.queue_id = uuid4().hex[:8]
        self.prefix = f"task={task_id or '-'} queue={self.queue_id}"
        self.total = total
        self.pending = total
        self.processing = 0
        self.succeeded = 0
        self.failed = 0
        self.workers = workers
        self.idle = workers
        _active_queues[self.queue_id] = self
        self.log("队列启动（每个任务本轮共用一条队列；处理中包含等待限流和请求重试）")

    def snapshot(self):
        return (f"{self.prefix} 本轮总数={self.total} 待领取={self.pending} "
                f"处理中={self.processing} 未完成={self.pending + self.processing} "
                f"成功={self.succeeded} 失败={self.failed} "
                f"存活worker={self.workers} 空闲worker={self.idle}")

    def log(self, event: str):
        print(f"[node4队列] {self.prefix} {event} | {self.snapshot()}", flush=True)
        queues = " ; ".join(q.snapshot() for q in _active_queues.values()) or "无"
        print(f"[node4队列总览] pid={os.getpid()} 本进程运行队列数={len(_active_queues)} | {queues}", flush=True)

    def claim(self, worker: str, shot: str):
        self.pending -= 1
        self.processing += 1
        self.idle -= 1
        self.log(f"{worker} 领取 {shot}")

    def finish(self, worker: str, shot: str, success: bool, elapsed: float, model: str, error: str = ""):
        self.processing -= 1
        self.idle += 1
        self.succeeded += int(success)
        self.failed += int(not success)
        result = "成功" if success else f"失败 原因={error[:120]}"
        self.log(f"{worker} {shot} {result} model={model} 总耗时（含排队/重试）={elapsed:.1f}s")

    def worker_exit(self, worker: str):
        self.workers -= 1
        self.idle -= 1
        self.log(f"{worker} 无待领取图片，结束工作；其他worker可能仍在处理")

    def close(self, completed: bool):
        _active_queues.pop(self.queue_id, None)
        self.workers = self.idle = 0
        self.log("本轮队列结束" if completed else "本轮队列取消或异常结束，未完成计数为终止时快照")
