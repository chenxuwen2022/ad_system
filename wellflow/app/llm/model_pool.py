"""VLM / 文本 LLM 模型轮询池。

模型列表通过 `fetch_model_options(capability=...)` 动态获取，支持 channel_id 渠道过滤、
preferred_model 偏好优先（优先使用某个模型，失败自动降级到池里其他模型）。

每次调用都会刷新：拉不到 → 直接抛 RuntimeError，让上层走重试或兜底。

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
        preferred_model: 偏好模型名（短名如 qwen3.5-flash）—— 放在轮询首位，失败自动降级
    """

    def __init__(
        self,
        *,
        capability: str | None = "text",
        channel_id: int | None = None,
        preferred_model: str | None = None,
    ) -> None:
        self._capability = capability
        self._channel_id = channel_id
        self._preferred_model = preferred_model
        self._states: list[_ModelState] = []
        self._refresh_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 模型列表管理
    # ------------------------------------------------------------------

    async def _ensure_models(self) -> None:
        """每次调用都强制从 new-api 拉最新的模型列表。

        注意：**不再保留任何旧列表**。拉不到 / 拉空 → 直接 raise，让上层走 fallback。
        """
        async with self._refresh_lock:
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
                f" (channel_id={self._channel_id}, preferred={self._preferred_model})",
                flush=True,
            )

    def _pick_start(self, n: int) -> int:
        """优先选 preferred_model 的 index；没找到则随机起始点。"""
        if n <= 1:
            return 0
        if self._preferred_model:
            for i, state in enumerate(self._states):
                # 支持短名匹配（"qwen3.5-flash" 命中 "provider/qwen3.5-flash"）
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
        from wellflow.app.llm.factory import get_llm_client, _strip_provider
        return get_llm_client("vlm", model_override=_strip_provider(state.model_key))

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
                _got_content = False
                try:
                    async for delta in client.stream_chat_with_images(
                        system=system, user=user, image_uris=image_uris,
                        reasoning_effort=reasoning_effort, extra_params=extra_params,
                    ):
                        if not _stream_started:
                            _stream_started = True
                            state.record_success()
                            print(f"[model-pool] ✅ {state.model_key} 流已建立", flush=True)
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


# ---------------------------------------------------------------------------
# 单例 / 工厂
# ---------------------------------------------------------------------------

_pool_instance: ModelPool | None = None


def get_model_pool(
    *,
    capability: str | None = "text",
    channel_id: int | None = None,
    preferred_model: str | None = "qwen3.5-flash",
) -> ModelPool:
    """拿到共享 ModelPool 实例（首次调用按参数创建，后续调用参数变化会重建）。

    默认配置：
      - capability='text'  → 拉文本/VLM 模型
      - channel_id=None    → 不按渠道过滤
      - preferred_model='qwen3.5-flash' → 优先用 qwen3.5-flash，失败降级

    node4 等需要独立配置的调用者传不同参数即可。
    """
    global _pool_instance
    need_rebuild = (
        _pool_instance is None
        or _pool_instance._capability != capability
        or _pool_instance._channel_id != channel_id
        or _pool_instance._preferred_model != preferred_model
    )
    if need_rebuild:
        _pool_instance = ModelPool(
            capability=capability,
            channel_id=channel_id,
            preferred_model=preferred_model,
        )
        print(
            f"[model-pool] 🏗️ 新建 ModelPool: capability={capability}"
            f" channel_id={channel_id} preferred={preferred_model}",
            flush=True,
        )
    return _pool_instance
