"""VLM / 文本 LLM 模型轮询池。

模型列表通过 `fetch_model_options(capability=...)` 动态获取，支持 channel_id 渠道过滤、
preferred_model 偏好优先（优先使用某个模型，失败自动降级到池里其他模型）。

模型列表按 task 缓存：任务启动时拉取一次，Node1～Node3、意图识别和 refine 共用。

所有请求统一走 new-api，渠道分发由 new-api 后台配置。
"""

from __future__ import annotations

import asyncio
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

from wellflow.app.api.model_options import fetch_model_options
from wellflow.app.config import settings


# ---------------------------------------------------------------------------
# 模型状态 + 熔断
# ---------------------------------------------------------------------------

@dataclass
class _ModelState:
    model_key: str
    failure_timestamps: list[float] = field(default_factory=list)
    cooldown_until: float = 0.0

    def _purge_old(self, now: float) -> None:
        cutoff = now - settings.model_pool_fail_window
        self.failure_timestamps = [t for t in self.failure_timestamps if t > cutoff]

    def is_available(self) -> bool:
        now = time.time()
        if now < self.cooldown_until:
            return False
        self._purge_old(now)
        return len(self.failure_timestamps) < settings.model_pool_fail_threshold

    def record_failure(self) -> None:
        now = time.time()
        self.failure_timestamps.append(now)
        self._purge_old(now)
        if len(self.failure_timestamps) >= settings.model_pool_fail_threshold:
            self.cooldown_until = now + settings.model_pool_cooldown
            print(f"[model-pool] 🔴 熔断 {self.model_key} — 冷却 {settings.model_pool_cooldown:.0f}s", flush=True)

    def record_success(self) -> None:
        self.failure_timestamps.clear()
        if self.cooldown_until:
            print(f"[model-pool] 🟢 恢复 {self.model_key}", flush=True)
        self.cooldown_until = 0.0


def _is_retryable_error(exc: Exception) -> bool:
    """可恢复错误 → 自动切下一个模型。"""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else 0
        return status == 429 or status >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError,
                            httpx.ConnectTimeout, httpx.ReadError,
                            httpx.WriteError, httpx.RequestError))


# ---------------------------------------------------------------------------
# ModelPool
# ---------------------------------------------------------------------------

class ModelPool:
    """从 new-api 动态拉取文本/VLM 模型池，配合熔断 + 偏好优先 + 失败自动切换。

    对外三个入口：chat (纯文本) / chat_with_images (多模态非流式) / stream_chat_with_images (多模态流式)。

    Args:
        capability: 从 new-api 拉哪些能力的模型（text / vlm / None=不过滤）
        channel_id: 只拉指定渠道的模型（None=全部渠道）
        preferred_model: 偏好模型名（短名如 qwen3.8-flash）—— 放在轮询首位，失败自动降级
    """

    def __init__(
        self,
        *,
        capability: str | None = "text",
        channel_id: int | None = None,
        preferred_model: str | None = None,
        task_id: str | None = None,
    ) -> None:
        self._capability = capability
        self._channel_id = channel_id
        self._preferred_model = preferred_model
        self._task_id = task_id or "__shared__"
        self._cache_models = task_id is not None
        self._states: list[_ModelState] = []
        self._refresh_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 模型列表管理
    # ------------------------------------------------------------------

    async def _ensure_models(self) -> None:
        """确保任务模型列表已加载；同一 task 生命周期内只请求 New API 一次。"""
        if self._cache_models and self._states:
            return
        async with self._refresh_lock:
            if self._cache_models and self._states:
                return
            raw_items = await fetch_model_options(
                self._capability, channel_id=self._channel_id,
            )
            if not raw_items:
                raise RuntimeError(
                    f"new-api 返回为空，没有可用的 {self._capability!r} 模型"
                )

            # 平滑重建：同名模型保留熔断状态
            old_map = {s.model_key: s for s in self._states}
            new_states: list[_ModelState] = []
            for opt in raw_items:
                key = opt.value  # 完整模型 id（带 provider 前缀，如有）
                if key in old_map:
                    new_states.append(old_map[key])
                else:
                    new_states.append(_ModelState(model_key=key))
            self._states = new_states
            print(
                f"[model-pool] ✅ 已从 new-api 拉取 {len(self._states)} 个 {self._capability!r} 模型"
                f" (task={self._task_id}, channel_id={self._channel_id}, preferred={self._preferred_model})",
                flush=True,
            )

    async def prepare(self) -> None:
        """任务启动阶段预加载模型列表。"""
        await self._ensure_models()

    def _pick_start(self, n: int) -> int:
        """优先选 preferred_model 的 index；没找到则随机起始点。"""
        if n <= 1:
            return 0
        if self._preferred_model:
            for i, state in enumerate(self._states):
                # 支持短名匹配（"qwen3.8-flash" 命中 "provider/qwen3.8-flash"）
                if state.model_key.endswith(f"/{self._preferred_model}") or \
                   state.model_key == self._preferred_model:
                    return i
        return random.randrange(n)

    def _next_available(self, start: int) -> _ModelState | None:
        n = len(self._states)
        if n == 0:
            return None
        for offset in range(n):
            state = self._states[(start + offset) % n]
            if state.is_available():
                return state
        return None

    def _build_client(self, state: _ModelState):
        """按 capability 推导 role：text → 'text'；其他（vlm / None / image）→ 'vlm'。

        🔴 历史 bug：这里之前硬编码成 "vlm"，导致 refine 节点（capability="text"）
        也拿到 role=vlm 的网关日志和超时策略，stream_chat 耗时多 3-5 倍。
        """
        from wellflow.app.llm.factory import get_llm_client, _strip_provider
        role = "text" if self._capability == "text" else "vlm"
        return get_llm_client(role, model_override=_strip_provider(state.model_key))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _with_retries_disabled(self) -> Iterator[None]:
        """临时关闭 NewApiGateway 的内部重试，让模型池自己接管 failover。"""
        from wellflow.app.llm.newapi_gateway import NewApiGateway
        original = NewApiGateway.MAX_RETRIES
        NewApiGateway.MAX_RETRIES = 0
        try:
            yield
        finally:
            NewApiGateway.MAX_RETRIES = original

    # ------------------------------------------------------------------
    # 非流式统一入口
    # ------------------------------------------------------------------

    async def _call_internal(
        self,
        method_name: str,                 # "chat" | "chat_with_images"
        *,
        system: str,
        user: str,
        image_uris: list[str] | None = None,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.3,
        reasoning_effort: str,
    ) -> tuple[Any, str]:
        """遍历动态模型池，失败自动切下一个。"""
        await self._ensure_models()

        if image_uris is not None:
            client_kwargs = dict(
                system=system, user=user, image_uris=image_uris,
                response_format=response_format, reasoning_effort=reasoning_effort,
            )
        else:
            client_kwargs = dict(
                system=system, user=user,
                response_format=response_format, temperature=temperature,
                reasoning_effort=reasoning_effort,
            )

        n = len(self._states)
        if n == 0:
            raise RuntimeError("模型池为空，无法调用 LLM")

        start = self._pick_start(n)
        last_exc: Exception | None = None
        for offset in range(n):
            state = self._next_available((start + offset) % n)
            if state is None:
                break
            client = self._build_client(state)
            print(f"[model-pool] 📤 {method_name} → {state.model_key}", flush=True)
            try:
                resp = await getattr(client, method_name)(**client_kwargs)
                state.record_success()
                print(f"[model-pool] ✅ {state.model_key}", flush=True)
                return (resp, state.model_key)
            except Exception as exc:
                last_exc = exc
                state.record_failure()
                label = "可恢复" if _is_retryable_error(exc) else "其他"
                print(f"[model-pool] ❌ {state.model_key} [{label}]: {type(exc).__name__}", flush=True)

        if last_exc:
            raise RuntimeError(f"模型池全部不可用: {last_exc}") from last_exc
        raise RuntimeError("模型池全部不可用")

    # ------------------------------------------------------------------
    # 对外三个入口
    # ------------------------------------------------------------------

    async def chat(
        self, *, system: str, user: str,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.3, reasoning_effort: str,
    ) -> tuple[Any, str]:
        with self._with_retries_disabled():
            return await self._call_internal(
                "chat", system=system, user=user,
                response_format=response_format, temperature=temperature,
                reasoning_effort=reasoning_effort,
            )

    async def chat_with_images(
        self, *, system: str, user: str, image_uris: list[str],
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str,
    ) -> tuple[Any, str]:
        with self._with_retries_disabled():
            return await self._call_internal(
                "chat_with_images", system=system, user=user, image_uris=image_uris,
                response_format=response_format, reasoning_effort=reasoning_effort,
            )

    async def stream_chat_with_images(
        self, *, system: str, user: str, image_uris: list[str],
        reasoning_effort: str, extra_params: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ):
        """流式多模态。failover 仅限连接建立期，流开始后不再切换。"""
        await self._ensure_models()

        with self._with_retries_disabled():
            n = len(self._states)
            if n == 0:
                raise RuntimeError("模型池为空，无法调用 LLM")

            start = self._pick_start(n)
            last_exc: Exception | None = None
            for offset in range(n):
                state = self._next_available((start + offset) % n)
                if state is None:
                    break
                client = self._build_client(state)
                print(f"[model-pool] 📤 stream → {state.model_key}", flush=True)

                _stream_started = False
                _first_content_logged = False
                _got_content = False
                _stream_start_ts: float | None = None
                try:
                    async for delta in client.stream_chat_with_images(
                        system=system, user=user, image_uris=image_uris,
                        reasoning_effort=reasoning_effort, extra_params=extra_params,
                        response_format=response_format,
                    ):
                        if not _stream_started:
                            _stream_started = True
                            state.record_success()
                            _stream_start_ts = time.time()
                            print(
                                f"[model-pool] ✅ {state.model_key} 流已建立 "
                                f"@ {time.strftime('%H:%M:%S')}",
                                flush=True,
                            )
                        has_text = isinstance(delta, dict) and delta.get("type") == "content" and delta.get("text")
                        if (has_text or delta) and not _first_content_logged and _stream_start_ts is not None:
                            _first_content_logged = True
                            _got_content = True
                            print(
                                f"[model-pool] ⚡ {state.model_key} 首 token 到达 "
                                f"@ {time.strftime('%H:%M:%S')} "
                                f"(流建立→首 token 耗时={time.time() - _stream_start_ts:.2f}s)",
                                flush=True,
                            )
                        if isinstance(delta, dict):
                            if delta.get("type") == "content" and delta.get("text"):
                                _got_content = True
                        elif delta:
                            _got_content = True
                        yield delta
                    # 流正常结束
                    if _stream_started:
                        if _got_content:
                            return
                        print(f"[model-pool] ⚠️ {state.model_key} 流结束但 content 为空，试下一个", flush=True)
                        state.record_failure()
                        continue
                    print(f"[model-pool] ⚠️ {state.model_key} 空流，试下一个", flush=True)
                    continue
                except Exception as exc:
                    last_exc = exc
                    if _stream_started:
                        print(f"[model-pool] 💥 {state.model_key} 流中断（已 yield），不再 failover", flush=True)
                        raise
                    state.record_failure()
                    label = "可恢复" if _is_retryable_error(exc) else "其他"
                    print(f"[model-pool] ❌ {state.model_key} 连接期失败 [{label}]: {type(exc).__name__}", flush=True)
                    continue

            if last_exc:
                raise RuntimeError(f"模型池全部不可用: {last_exc}") from last_exc
            raise RuntimeError("模型池全部不可用")

    async def stream_chat(
        self, *, system: str, user: str,
        reasoning_effort: str, extra_params: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ):
        """纯文本流式调用，复用当前 task 已加载的模型列表。"""
        await self._ensure_models()
        with self._with_retries_disabled():
            n = len(self._states)
            start = self._pick_start(n)
            last_exc: Exception | None = None
            for offset in range(n):
                state = self._next_available((start + offset) % n)
                if state is None:
                    break
                client = self._build_client(state)
                try:
                    async for delta in client.stream_chat(
                        system=system, user=user, reasoning_effort=reasoning_effort,
                        extra_params=extra_params, response_format=response_format,
                    ):
                        yield delta
                    state.record_success()
                    return
                except Exception as exc:
                    last_exc = exc
                    state.record_failure()
            if last_exc:
                raise RuntimeError(f"模型池全部不可用: {last_exc}") from last_exc
            raise RuntimeError("模型池全部不可用")


# ---------------------------------------------------------------------------
# 单例 / 工厂
# ---------------------------------------------------------------------------

_pool_instances: dict[str, ModelPool] = {}


def get_model_pool(
    *,
    capability: str | None = "text",
    channel_id: int | None = None,          # None → 从 config.settings.llm_channel_id 读
    preferred_model: str | None = "qwen3.8-flash",
    task_id: str | None = None,
) -> ModelPool:
    """获取 task 级 ModelPool；未传 task_id 的独立服务沿用共享池。

    默认配置：
      - capability='text'  → 拉 text/VLM 模型
      - channel_id=None    → 走 settings.llm_channel_id（默认 4，LLM/VLM 统一渠道）
      - preferred_model='qwen3.8-flash' → 优先用 qwen3.8-flash，失败降级

    node4 生图模型由前端指定，不使用模型池。
    """
    channel_id = channel_id or settings.llm_channel_id  # None → config 统一渠道
    key = task_id or "__shared__"
    pool = _pool_instances.get(key)
    need_rebuild = pool is None or pool._capability != capability or pool._channel_id != channel_id
    if need_rebuild:
        pool = ModelPool(
            capability=capability,
            channel_id=channel_id,
            preferred_model=preferred_model,
            task_id=task_id,
        )
        _pool_instances[key] = pool
        print(
            f"[model-pool] 🏗️ 新建 ModelPool: capability={capability}"
            f" task={key} channel_id={channel_id} preferred={preferred_model}",
            flush=True,
        )
    return pool


async def prepare_task_models(task_id: str) -> ModelPool:
    """任务启动入口：只在这里预加载一次 text 模型目录。"""
    pool = get_model_pool(task_id=task_id)
    await pool.prepare()
    return pool


def clear_task_models(task_id: str) -> None:
    _pool_instances.pop(task_id, None)
