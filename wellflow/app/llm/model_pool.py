"""通用 VLM 模型轮询池 —— 支持文本 / 多模态 / 流式三种调用。

设计要点：
  - 国内 6 个模型优先遍历，全部熔断才兜底海外 gemini
  - 捕获 429/超时/连接异常/5xx → 自动切下一个模型
  - 单模型 10s 内连续 2 次失败 → 临时熔断 30s 自动恢复
  - 轮询起点每次调用随机偏移（时间戳取模），避免 intent / Node 并发互相干扰
  - 流式 failover 仅限「连接建立期」（first SSE chunk 前），流开始后不再切换
  - 所有模型共用同一套网关，reasoning_effort 参数由调用方传入

配置来源：wellflow.app.config.settings
  - model_pool_domestic_models      国内模型列表
  - model_pool_overseas_fallback    海外兜底模型
  - model_pool_fail_threshold       连续失败熔断阈值
  - model_pool_fail_window          失败统计时间窗口（秒）
  - model_pool_cooldown             熔断后冷却时间（秒）
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from wellflow.app.config import settings


# ---------------------------------------------------------------------------
# 异常判断
# ---------------------------------------------------------------------------

def _is_retryable_error(exc: Exception) -> bool:
    """判断某个异常是否属于「应该切下一个模型」的可恢复错误。"""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else 0
        if status == 429 or status >= 500:
            return True
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError,
                        httpx.ConnectTimeout, httpx.ReadError,
                        httpx.WriteError, httpx.RequestError)):
        return True
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        if any(k in msg for k in ("503", "429", "502", "504", "timeout",
                                   "temporarily", "unavailable", "rate",
                                   "connect", "max_retries", "retry")):
            return True
    return False


# ---------------------------------------------------------------------------
# 熔断状态
# ---------------------------------------------------------------------------

@dataclass
class _ModelState:
    model_key: str
    short_name: str
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
            print(f"[model-pool] 🔴 熔断 {self.model_key} — "
                  f"{settings.model_pool_fail_window:.0f}s 内 {len(self.failure_timestamps)} 次失败，"
                  f"冷却 {settings.model_pool_cooldown:.0f}s",
                  flush=True)

    def record_success(self) -> None:
        self.failure_timestamps.clear()
        if self.cooldown_until:
            print(f"[model-pool] 🟢 恢复 {self.model_key}", flush=True)
        self.cooldown_until = 0.0


# ---------------------------------------------------------------------------
# 模型池
# ---------------------------------------------------------------------------

class ModelPool:
    """通用 VLM 模型轮询池。

    三种调用方式：
      pool.chat(...)                       — 纯文本（意图识别用）
      pool.chat_with_images(...)           — 多模态非流式（Node1/Node2/Node3 非流式路径）
      pool.stream_chat_with_images(...)    — 多模态流式（Node1/Node2/Node3 流式路径）
    """

    def __init__(self) -> None:
        from wellflow.app.llm.factory import _strip_provider

        domestic = settings.model_pool_domestic_models
        fallback = settings.model_pool_overseas_fallback

        self._domestic: list[_ModelState] = [
            _ModelState(model_key=m, short_name=_strip_provider(m))
            for m in domestic
        ]
        self._fallback = _ModelState(
            model_key=fallback,
            short_name=_strip_provider(fallback),
        )
        # round-robin 全局递增索引（每次调用 +1，避免并发互相干扰）
        self._rr_idx = 0

    def _pick_start(self, n: int) -> int:
        """round-robin 全局递增。每次调用返回下一个起始位置。"""
        idx = self._rr_idx % n
        self._rr_idx += 1
        return idx

    def _next_available(self, group: list[_ModelState], start: int) -> _ModelState | None:
        """从 start 位置开始找下一个可用模型。"""
        n = len(group)
        for offset in range(n):
            idx = (start + offset) % n
            state = group[idx]
            if state.is_available():
                return state
            remain = max(state.cooldown_until - time.time(), 0.0)
            if remain > 0:
                print(f"[model-pool] ⏭️ 跳过 {state.model_key} (冷却中 {remain:.0f}s)", flush=True)
            else:
                print(f"[model-pool] ⏭️ 跳过 {state.model_key} ({settings.model_pool_fail_window:.0f}s 内失败过多)", flush=True)
        return None

    def _build_client(self, state: _ModelState):
        from wellflow.app.llm.factory import get_llm_client
        return get_llm_client("vlm", model_override=state.short_name)

    async def _run_fallback(self, callable_name: str, *args, **kwargs) -> tuple[Any, str]:
        """国内池全挂时才走到这里。"""
        print(f"[model-pool] ⚠️ 国内全部不可用，尝试海外兜底 {self._fallback.model_key}", flush=True)
        fn = getattr(self, callable_name + "_internal")
        result = await fn([self._fallback], *args, **kwargs)
        if result is not None:
            return result
        raise RuntimeError(
            f"模型池全部不可用（国内 {len(self._domestic)} + 海外兜底）"
        )

    # ------------------------------------------------------------------
    # 1. 纯文本 chat（意图识别用）
    # ------------------------------------------------------------------

    async def chat(
        self,
        *,
        system: str,
        user: str,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.3,
        reasoning_effort: str | None = None,
    ) -> tuple[Any, str]:
        from wellflow.app.llm.ofox_gateway import OfoxGateway
        original_retries = OfoxGateway.MAX_RETRIES
        OfoxGateway.MAX_RETRIES = 0

        try:
            result = await self._chat_internal(
                self._domestic, system=system, user=user,
                response_format=response_format, temperature=temperature,
                reasoning_effort=reasoning_effort,
            )
            if result is not None:
                return result
            return await self._run_fallback("chat",
                system=system, user=user,
                response_format=response_format, temperature=temperature,
                reasoning_effort=reasoning_effort,
            )
        finally:
            OfoxGateway.MAX_RETRIES = original_retries

    async def _chat_internal(
        self, group: list[_ModelState],
        *, system: str, user: str,
        response_format: dict[str, Any] | None,
        temperature: float, reasoning_effort: str | None,
    ) -> tuple[Any, str] | None:
        n = len(group)
        start = self._pick_start(n)
        last_exc: Exception | None = None

        for offset in range(n):
            state = self._next_available(group, (start + offset) % n)
            if state is None:
                break

            client = self._build_client(state)
            print(f"[model-pool] 📤 chat → {state.model_key}", flush=True)
            try:
                resp = await client.chat(
                    system=system, user=user,
                    response_format=response_format,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                )
                state.record_success()
                print(f"[model-pool] ✅ {state.model_key}", flush=True)
                return (resp, state.model_key)
            except Exception as exc:
                last_exc = exc
                state.record_failure()
                label = "可恢复" if _is_retryable_error(exc) else "其他"
                print(f"[model-pool] ❌ {state.model_key} [{label}]: {type(exc).__name__}", flush=True)

        if last_exc:
            print(f"[model-pool] ⚠️ 本组全部失败", flush=True)
        return None

    # ------------------------------------------------------------------
    # 2. 多模态非流式 chat_with_images
    # ------------------------------------------------------------------

    async def chat_with_images(
        self,
        *,
        system: str,
        user: str,
        image_uris: list[str],
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[Any, str]:
        from wellflow.app.llm.ofox_gateway import OfoxGateway
        original_retries = OfoxGateway.MAX_RETRIES
        OfoxGateway.MAX_RETRIES = 0

        try:
            result = await self._chat_with_images_internal(
                self._domestic, system=system, user=user,
                image_uris=image_uris,
                response_format=response_format,
                reasoning_effort=reasoning_effort,
            )
            if result is not None:
                return result
            return await self._run_fallback("chat_with_images",
                system=system, user=user,
                image_uris=image_uris,
                response_format=response_format,
                reasoning_effort=reasoning_effort,
            )
        finally:
            OfoxGateway.MAX_RETRIES = original_retries

    async def _chat_with_images_internal(
        self, group: list[_ModelState],
        *, system: str, user: str, image_uris: list[str],
        response_format: dict[str, Any] | None,
        reasoning_effort: str | None,
    ) -> tuple[Any, str] | None:
        n = len(group)
        start = self._pick_start(n)
        last_exc: Exception | None = None

        for offset in range(n):
            state = self._next_available(group, (start + offset) % n)
            if state is None:
                break

            client = self._build_client(state)
            n_img = len(image_uris)
            print(f"[model-pool] 📤 chat_with_images → {state.model_key} (imgs={n_img})", flush=True)
            try:
                resp = await client.chat_with_images(
                    system=system, user=user, image_uris=image_uris,
                    response_format=response_format,
                    reasoning_effort=reasoning_effort,
                )
                state.record_success()
                print(f"[model-pool] ✅ {state.model_key}", flush=True)
                return (resp, state.model_key)
            except Exception as exc:
                last_exc = exc
                state.record_failure()
                label = "可恢复" if _is_retryable_error(exc) else "其他"
                print(f"[model-pool] ❌ {state.model_key} [{label}]: {type(exc).__name__}", flush=True)

        if last_exc:
            print(f"[model-pool] ⚠️ 本组全部失败", flush=True)
        return None

    # ------------------------------------------------------------------
    # 3. 多模态流式 —— 连接期 failover，流开始后不再切换
    # ------------------------------------------------------------------

    async def stream_chat_with_images(
        self,
        *,
        system: str,
        user: str,
        image_uris: list[str],
        reasoning_effort: str | None = None,
    ):
        """流式多模态 VLM 调用，yield {"type": "thinking"|"content", "text": "..."}。

        Failover 策略：
          - 在第一个 SSE data chunk 到达之前发生异常 → 自动切下一个模型
          - 流已开始 yield chunk 后发生断裂 → 不再切换，直接抛异常给上层
        """
        from wellflow.app.llm.ofox_gateway import OfoxGateway
        original_retries = OfoxGateway.MAX_RETRIES
        OfoxGateway.MAX_RETRIES = 0

        try:
            gen = self._stream_chat_with_images_internal(
                self._domestic, system=system, user=user,
                image_uris=image_uris, reasoning_effort=reasoning_effort,
            )
            async for item in gen:
                yield item
        except RuntimeError:
            # 国内全部失败 → 海外兜底
            print(f"[model-pool] ⚠️ 国内全部不可用，尝试海外兜底 stream", flush=True)
            gen = self._stream_chat_with_images_internal(
                [self._fallback], system=system, user=user,
                image_uris=image_uris, reasoning_effort=reasoning_effort,
            )
            async for item in gen:
                yield item
        finally:
            OfoxGateway.MAX_RETRIES = original_retries

    async def _stream_chat_with_images_internal(
        self, group: list[_ModelState],
        *, system: str, user: str, image_uris: list[str],
        reasoning_effort: str | None,
    ):
        """内部实现：遍历模型，连接期 failover，流式 yield。"""
        n = len(group)
        start = self._pick_start(n)
        last_exc: Exception | None = None

        for offset in range(n):
            state = self._next_available(group, (start + offset) % n)
            if state is None:
                break

            client = self._build_client(state)
            n_img = len(image_uris)
            print(f"[model-pool] 📤 stream → {state.model_key} (imgs={n_img}, eff={reasoning_effort})", flush=True)

            _stream_started = False
            try:
                async for delta in client.stream_chat_with_images(
                    system=system, user=user, image_uris=image_uris,
                    reasoning_effort=reasoning_effort,
                ):
                    if not _stream_started:
                        _stream_started = True
                        state.record_success()
                        print(f"[model-pool] ✅ {state.model_key} 流已建立", flush=True)
                    yield delta
                # 流正常结束（[DONE]）
                if _stream_started:
                    return  # 正常结束，不再遍历其他模型
                # 没 yield 任何东西就退出了？可能是模型返回空流
                print(f"[model-pool] ⚠️ {state.model_key} 空流，试下一个", flush=True)
                continue
            except Exception as exc:
                last_exc = exc
                if _stream_started:
                    # 流已开始后断裂 → 不再切换，直接抛出
                    print(f"[model-pool] 💥 {state.model_key} 流中断（已 yield），不再 failover", flush=True)
                    raise
                # 连接期失败 → 切下一个模型
                state.record_failure()
                label = "可恢复" if _is_retryable_error(exc) else "其他"
                print(f"[model-pool] ❌ {state.model_key} 连接期失败 [{label}]: {type(exc).__name__}", flush=True)
                continue

        # 全部遍历完
        if last_exc:
            print(f"[model-pool] ⚠️ 本组全部失败", flush=True)
        raise RuntimeError("模型池全部不可用")


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_pool_instance: ModelPool | None = None


def get_model_pool() -> ModelPool:
    global _pool_instance
    if _pool_instance is None:
        _pool_instance = ModelPool()
    return _pool_instance


# 兼容旧 import（intent_classifier 还在用 get_intent_pool）
def get_intent_pool() -> ModelPool:
    return get_model_pool()
