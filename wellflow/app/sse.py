"""SSE 推送器（final.md 第 11.3 节）。

架构：事件驱动为主，DB 轮询兜底。

  Graph 节点落 DB
       │
       ▼
  event_bus.publish(task_id, "phase", {...})   ← 零延迟通知
       │
       ▼
  SSE 端点 await queue.get()                    ← 阻塞等，不查 DB
       │
       ▼
  推送给前端 event: phase / interrupt / done / error

  如果 30 秒没收到事件（graph 还在跑但没新产出），
  做一次 DB 兜底查询 + 发个 heartbeat，然后继续等 queue。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from wellflow.app.database import get_db
from wellflow.app.repositories.task_repo import TaskRepo


router = APIRouter(tags=["SSE"])

# DB 兜底间隔（秒）——事件驱动超过这个时间没收到事件才查一次 DB
_DB_FALLBACK_INTERVAL = 30
# SSE heartbeat 间隔（秒）——防止代理掐连接
_HEARTBEAT_INTERVAL = 25


@router.get("/api/tasks/{task_id}/stream")
async def stream_task(task_id: str, request: Request, db: Session = Depends(get_db)):
    """订阅任务进度事件。事件驱动为主，DB 轮询兜底。"""

    async def event_generator():
        from wellflow.app.event_bus import drain_and_subscribe
        from wellflow.app.database import SessionLocal

        # 清空旧积压，订阅事件队列
        q = await drain_and_subscribe(task_id)

        last_phase: str | None = None
        last_interrupt_node: str | None = None
        end_emitted = False  # done/error 是否已经推送过

        # 先做一次初始 DB 查询，拿到立即状态（兼容"任务已结束但前端刚订阅"的情况）
        fresh_db = SessionLocal()
        try:
            init_task = TaskRepo(fresh_db).get(task_id)
        finally:
            fresh_db.close()
        if init_task:
            last_phase = init_task.phase
            interrupt = init_task.interrupt_json
            if init_task.phase in ("done", "failed"):
                yield _sse("done" if init_task.phase == "done" else "error", {"phase": init_task.phase})
                end_emitted = True
            elif interrupt:
                last_interrupt_node = interrupt.get("node")
                yield _sse("phase", {"phase": init_task.phase})
                yield _sse("interrupt", interrupt)

        # 主循环：阻塞等事件，不查 DB
        while not end_emitted:
            if await request.is_disconnected():
                break

            try:
                # 阻塞等事件，最长 _DB_FALLBACK_INTERVAL 秒
                # asyncio.wait_for 实现超时
                event = await asyncio.wait_for(q.get(), timeout=_DB_FALLBACK_INTERVAL)
                event_type = event.get("type")
                event_data = event.get("data", {})
                print(f"[sse] task={task_id} ← queue got type={event_type}", flush=True)

                if event_type == "phase":
                    phase_val = event_data.get("phase")
                    if phase_val and phase_val != last_phase:
                        last_phase = phase_val
                        yield _sse("phase", {"phase": phase_val})

                elif event_type == "interrupt":
                    interrupt_node = event_data.get("node")
                    if interrupt_node != last_interrupt_node:
                        last_interrupt_node = interrupt_node
                        # phase 已由后端在 interrupt 之前单独推送过了，这里只透传 interrupt
                        yield _sse("interrupt", event_data)

                elif event_type == "thinking_chunk":
                    # VLM 流式思考片段，直接透传给前端
                    yield _sse("thinking_chunk", event_data)

                elif event_type == "report_chunk":
                    # VLM 流式输出片段，直接透传
                    yield _sse("report_chunk", event_data)

                elif event_type == "report_chunk_done":
                    yield _sse("report_chunk_done", event_data)

                elif event_type == "error":
                    yield _sse("error", {"phase": "failed", "message": event_data.get("message", "")})
                    end_emitted = True

            except asyncio.TimeoutError:
                # ⏰ 事件驱动超时——graph 可能在跑但没 publish（或 queue 丢了）
                # 做一次 DB 兜底查询
                fresh_db2 = SessionLocal()
                try:
                    db_task = TaskRepo(fresh_db2).get(task_id)
                finally:
                    fresh_db2.close()

                if not db_task:
                    yield _sse("error", {"message": f"task {task_id} 不存在"})
                    break

                # 检查是否结束
                if db_task.phase in ("done", "failed"):
                    yield _sse("done" if db_task.phase == "done" else "error", {"phase": db_task.phase})
                    end_emitted = True
                    break

                # 检查 phase 变了没
                if db_task.phase != last_phase:
                    last_phase = db_task.phase
                    yield _sse("phase", {"phase": db_task.phase})

                # 检查 interrupt 变了没
                interrupt = db_task.interrupt_json
                interrupt_node = interrupt.get("node") if interrupt else None
                if interrupt and interrupt_node != last_interrupt_node:
                    last_interrupt_node = interrupt_node
                    yield _sse("interrupt", interrupt)
                elif not interrupt and last_interrupt_node is not None:
                    last_interrupt_node = None

                # 发 heartbeat，防止代理掐 SSE 连接
                yield f": heartbeat\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
