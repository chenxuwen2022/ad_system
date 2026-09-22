# -*- coding: utf-8 -*-
"""三库(穿搭/场景/模特)共享的异步任务设施(统一优化只改这一个文件)。

统一封装:任务文件读写 / pid 判死 / TTL 清理 / sweep 行联动 / 后台协程注册防 GC /
生成图下载(SSL 降级重试 + 图片魔数校验)。
穿搭库(outfit)与场景库(scenes)已切换复用本模块,模特库(mannequins)异步化直接使用。
"""

from __future__ import annotations

import asyncio
import base64
import json as json_mod
import os
import threading
import time
from pathlib import Path
from typing import Callable


class TaskStore:
    """任务状态文件存储(save/load/sweep),线程锁保护写。

    判死规则:status=processing 且(pid ≠ 当前进程 或 同 pid 超 stale_seconds)→ failed;
    mtime 超 ttl_seconds → 删除文件。判死时若文件含 row_id 且提供 mark_row_failed
    回调,联动把 DB 行标 failed。
    """

    def __init__(
        self,
        task_dir: Path,
        *,
        pid: str | None = None,
        ttl_seconds: float = 24 * 3600,
        stale_seconds: float = 60 * 60,
        row_id_field: str = "outfit_id",
        mark_row_failed: Callable[[int], None] | None = None,
    ):
        self.task_dir = task_dir
        self.pid = pid or str(os.getpid())
        self.ttl_seconds = ttl_seconds
        self.stale_seconds = stale_seconds
        self.row_id_field = row_id_field
        self.mark_row_failed = mark_row_failed
        self._lock = threading.RLock()

    def _path(self, task_id: str) -> Path:
        return self.task_dir / f"{task_id}.json"

    def save(self, task: dict) -> None:
        with self._lock:
            self.task_dir.mkdir(parents=True, exist_ok=True)
            self._path(task["task_id"]).write_text(
                json_mod.dumps(task, ensure_ascii=False), encoding="utf-8")

    def load(self, task_id: str) -> dict | None:
        p = self._path(task_id)
        if p.exists():
            try:
                return json_mod.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def sweep(self) -> None:
        """任务文件自愈(同步实现,调用方应放入线程池执行)。"""
        if not self.task_dir.exists():
            return
        now = time.time()
        for p in self.task_dir.glob("*.json"):
            try:
                t = json_mod.loads(p.read_text(encoding="utf-8"))
            except Exception:
                t = None
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            expired = now - mtime > self.ttl_seconds
            stale_processing = (
                isinstance(t, dict)
                and t.get("status") == "processing"
                and (t.get("pid") != self.pid or now - mtime > self.stale_seconds)
            )
            if not expired and not stale_processing:
                continue
            if stale_processing:
                row_id = (t or {}).get(self.row_id_field)
                if row_id and self.mark_row_failed:
                    self.mark_row_failed(int(row_id))
                t["status"] = "failed"
                t["error"] = "服务重启或任务超时中断,请重试"
                try:
                    p.write_text(json_mod.dumps(t, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
            else:
                try:
                    p.unlink()
                except OSError:
                    pass


class BackgroundTasks:
    """后台协程注册表:create_task 并持有引用,防任务被 GC。"""

    def __init__(self):
        self._tasks: set = set()

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)


def download_image(url: str, model: str) -> str:
    """下载网关返回的生成图 url → b64(SSL 降级重试 + 图片魔数校验)。"""
    import ssl
    import urllib.request as _ur

    data = None
    errors = []
    contexts = [None, ssl._create_unverified_context()]
    for ctx in contexts:
        try:
            with _ur.urlopen(url, timeout=60, context=ctx) as resp:
                data = resp.read()
            break
        except Exception as e:
            errors.append(str(e)[:90])
    if data is None:
        raise RuntimeError(f"{model}: 下载生成图失败: {errors[-1]}")
    if len(data) < 64 or data[:8] not in (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"):
        raise RuntimeError(f"{model}: 下载内容不是图片({data[:16]!r})")
    return base64.b64encode(data).decode()
