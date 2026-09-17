"""Ofox 网关实现（OpenAI 协议）。

所有模型走 /v1/chat/completions，通过 extra_params 透传模型特定扩展参数。
reasoning_effort 参数可控制 Gemini 等推理模型的思考强度。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from wellflow.app.llm.base import BaseLLMClient, LLMResponse


class OfoxGateway(BaseLLMClient):
    """Ofox 网关：纯 OpenAI 协议，透传 extra_params。"""

    MAX_RETRIES = 2
    RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(
        self,
        model: str = "qwen-max",
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        proxy_url: str | None = None,
    ):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.proxy_url = proxy_url

    async def chat(
        self,
        system: str,
        user: str,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.3,
        reasoning_effort: str | None = None,
        extra_params: dict[str, Any] | None = None,
        *,
        user_content: Any = None,
    ) -> LLMResponse:
        return await self._openai_chat(
            system=system,
            user=user,
            response_format=response_format,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            extra_params=extra_params,
            user_content=user_content,
        )

    async def chat_with_images(
        self,
        system: str,
        user: str,
        image_uris: list[str],
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> LLMResponse:
        user_content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for uri in image_uris:
            user_content.append({"type": "image_url", "image_url": {"url": uri}})
        self._log_payload_size(user_content)

        return await self._openai_chat(
            system=system,
            user=user,
            response_format=response_format,
            temperature=0.3,
            reasoning_effort=reasoning_effort,
            extra_params=extra_params,
            user_content=user_content,
        )

    # ------------------------------------------------------------------
    # 流式多模态调用（异步生成器）
    # ------------------------------------------------------------------

    async def stream_chat_with_images(
        self,
        system: str,
        user: str,
        image_uris: list[str],
        reasoning_effort: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ):
        """流式多模态 VLM 调用，yield delta 文本片段。"""
        user_content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for uri in image_uris:
            user_content.append({"type": "image_url", "image_url": {"url": uri}})
        self._log_payload_size(user_content)

        async for delta in self._openai_chat_stream(
            system=system,
            user=user,
            reasoning_effort=reasoning_effort,
            extra_params=extra_params,
            user_content=user_content,
        ):
            yield delta

    def _log_payload_size(self, user_content: Any) -> None:
        """打印多模态请求的 payload 尺寸摘要，方便排查模型 image_url 截断问题。"""
        if not isinstance(user_content, list):
            return
        image_blocks = [b for b in user_content if isinstance(b, dict) and b.get("type") == "image_url"]
        if not image_blocks:
            return
        total_b64_chars = 0
        max_single_b64 = 0
        for block in image_blocks:
            url = block.get("image_url", {}).get("url", "")
            comma_idx = url.find(",")
            b64_len = len(url) - (comma_idx + 1) if comma_idx >= 0 else len(url)
            total_b64_chars += b64_len
            if b64_len > max_single_b64:
                max_single_b64 = b64_len
        est_raw_mb = total_b64_chars * 3 / 4 / 1024 / 1024
        print(
            f"[llm-payload] 📦 {len(image_blocks)} 张图, "
            f"base64 合计 {total_b64_chars:,} chars (≈ {est_raw_mb:.2f}MB raw), "
            f"单张最长 {max_single_b64:,} chars b64",
            flush=True,
        )

    # ------------------------------------------------------------------
    # OpenAI 协议核心实现
    # ------------------------------------------------------------------

    async def _openai_chat(
        self,
        system: str,
        user: str,
        response_format: dict[str, Any] | None,
        temperature: float,
        reasoning_effort: str | None,
        extra_params: dict[str, Any] | None,
        user_content: Any,
    ) -> LLMResponse:
        if not self.base_url:
            raise RuntimeError(
                f"LLM 网关 base_url 未配置（model={self.model}）。"
                "请在 .env 中设置 NEWAPI_API_KEY。"
            )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
        ]
        if user_content is not None:
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": user})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }

        # 深度思考 / Reasoning 控制（OpenAI 协议透传，Gemini 等模型支持）
        # 约定：reasoning_effort 为 "none" 时**不传该字段**，让模型用默认值（通常关闭推理）。
        # OpenAI 官方合法值为 low/medium/high；传 "none" 字符串可能被网关忽略后仍返回 thinking。
        if reasoning_effort is not None and reasoning_effort != "none":
            payload["reasoning_effort"] = reasoning_effort

        if response_format and response_format.get("type") == "json_object":
            payload["response_format"] = {"type": "json_object"}

        # 通用透传扩展参数
        if extra_params:
            payload.update(extra_params)

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        print(f"[llm] POST {self.base_url}/chat/completions model={self.model} stream=False", flush=True)
        print(f"[llm] payload keys={list(payload.keys())} response_format={payload.get('response_format')}", flush=True)

        last_exc: Exception | None = None
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout, proxy=self.proxy_url) as client:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                    if resp.status_code in self.RETRYABLE_STATUS and attempt < self.MAX_RETRIES:
                        last_exc = httpx.HTTPStatusError(str(resp.status_code), request=resp.request, response=resp)
                        continue
                    resp.raise_for_status()
                    if resp.status_code >= 400:
                        print(f"[llm] ❌ HTTP {resp.status_code} body={resp.text[:500]}", flush=True)
                    data = resp.json()
                # DEBUG: 打印原始响应结构，排查 content 为 None 的问题
                raw_content = data.get("choices", [{}])[0].get("message", {}).get("content")
                print(f"[llm] raw message.content type={type(raw_content).__name__} "
                      f"len={len(raw_content) if isinstance(raw_content, str) else 'N/A'} "
                      f"value={repr(raw_content)[:200]}", flush=True)
                # 兼容：content 为 None 时尝试从 parts 或其他字段提取
                if raw_content is None:
                    msg = data.get("choices", [{}])[0].get("message", {})
                    # Google/Gemini 可能返回 parts 数组
                    if isinstance(msg.get("parts"), list):
                        parts_text = [p.get("text", "") for p in msg["parts"] if isinstance(p, dict)]
                        raw_content = "\n".join(parts_text)
                        print(f"[llm] fallback: 从 parts 提取, len={len(raw_content)}", flush=True)
                    # 或者 content 本身是 dict（有 text key）
                    elif isinstance(msg.get("content"), dict):
                        raw_content = msg["content"].get("text", "")
                    else:
                        raw_content = ""
                # 提取 thinking/reasoning 文本（非流式）
                thinking_text = None
                try:
                    msg = data.get("choices", [{}])[0].get("message", {})
                    # ofox: reasoning_details=[{'type': 'reasoning.text', 'text': '...'}, ...]
                    rd_list = msg.get("reasoning_details")
                    if isinstance(rd_list, list):
                        thinking_parts = []
                        for rd in rd_list:
                            if isinstance(rd, dict) and rd.get("type") == "reasoning.text" and rd.get("text"):
                                thinking_parts.append(rd["text"])
                        if thinking_parts:
                            thinking_text = "".join(thinking_parts)
                    # 兼容 OpenAI: reasoning_content / thinking 字段
                    if not thinking_text:
                        thinking_text = msg.get("reasoning_content") or msg.get("thinking") or None
                except Exception:
                    pass

                return LLMResponse(
                    content=raw_content,
                    raw=data,
                    usage=data.get("usage", {}),
                    model=data.get("model", self.model),
                    thinking=thinking_text,
                )
            except Exception as e:
                last_exc = e
                if attempt >= self.MAX_RETRIES:
                    raise
        raise last_exc  # type: ignore[misc]

    async def _openai_chat_stream(
        self,
        system: str,
        user: str,
        reasoning_effort: str | None,
        extra_params: dict[str, Any] | None,
        user_content: Any,
    ):
        """OpenAI SSE 流式实现，yield 合并后的 delta 文本片段。

        内置 micro-batching：把 20ms 时间窗口内的多个小 SSE chunk（常见于 OpenAI 模型
        每个 token 就一个 chunk）合并成一个 yield，避免前端"一个字一个字蹦"。
        对 Gemini 等本身 chunk 粒度就较大的模型无副作用（合并窗口内只有 1 个 chunk）。
        """
        _time = __import__("time")
        _FLUSH_INTERVAL = 0.02  # 20ms → ~50fps，肉眼看不到延迟但不会一个字一个字蹦

        if not self.base_url:
            raise RuntimeError(
                f"LLM 网关 base_url 未配置（model={self.model}）。"
                "请在 .env 中设置 NEWAPI_API_KEY。"
            )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
        ]
        if user_content is not None:
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": user})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }

        # ✅ ofox 网关流式多模态 + reasoning_effort 先尝试传
        # 如果底层不支持会抛异常，Node1 的流式调用方会 catch 并降级到非流式
        # 约定同非流式：effort=="none" 时**不传**，避免模型忽略后仍返回 thinking
        if reasoning_effort is not None and reasoning_effort != "none":
            payload["reasoning_effort"] = reasoning_effort

        if extra_params:
            payload.update(extra_params)

        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # 仅对「连接建立阶段」做重试（与非流式 _openai_chat 保持一致）
        # 流已开始后再中断（ReadError 发生在 aiter_lines 内部）不重试，交由上层处理
        _RETRYABLE_NETWORK_ERRORS = (httpx.ReadError, httpx.WriteError, httpx.ConnectError, httpx.ConnectTimeout, httpx.HTTPStatusError)

        last_exc: Exception | None = None
        for attempt in range(self.MAX_RETRIES + 1):
            _stream_started = False  # resp 已拿到（HTTP headers 已读完）= True

            # ── micro-batching 缓冲区 ──
            _content_buf = ""
            _thinking_buf = ""
            _last_flush_ts = _time.time()

            async def _flush():
                """把缓冲区里累积的 delta 一次性 yield 出去。"""
                nonlocal _content_buf, _thinking_buf, _last_flush_ts
                if _thinking_buf:
                    yield {"type": "thinking", "text": _thinking_buf}
                    _thinking_buf = ""
                if _content_buf:
                    yield {"type": "content", "text": _content_buf}
                    _content_buf = ""
                _last_flush_ts = _time.time()

            try:
                async with httpx.AsyncClient(timeout=None, proxy=self.proxy_url) as client:
                    _t0 = _time.time()
                    print(f"[llm] → POST {self.base_url}/chat/completions model={self.model} stream=True ...", flush=True)
                    async with client.stream(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as resp:
                        resp.raise_for_status()
                        _stream_started = True  # 连接已就绪，标志切换后不再重试
                        _t1 = _time.time()
                        print(f"[llm] ← HTTP {resp.status_code}, 首字节到达 +{_t1 - _t0:.2f}s", flush=True)
                        _first_data_ts = None
                        async for raw_line in resp.aiter_lines():
                            line = raw_line.strip()
                            if not line:
                                continue
                            if not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                break
                            if _first_data_ts is None:
                                _first_data_ts = _time.time()
                                print(f"[llm] ← 第一个 SSE data 行到达 +{_first_data_ts - _t0:.2f}s (其中 HTTP headers={_t1 - _t0:.2f}s)", flush=True)
                            try:
                                data = json.loads(data_str)
                            except Exception:
                                continue
                            choice0 = data.get("choices", [{}])[0] if data.get("choices") else {}
                            delta_obj = choice0.get("delta", {})

                            # 简要日志：keys + 是否有 thinking/content
                            # if delta_obj:
                            #     has_rd = bool(delta_obj.get("reasoning_details"))
                            #     has_content = bool(delta_obj.get("content"))
                            #     print(f"[llm-stream] keys={list(delta_obj.keys())} rd={has_rd} content={has_content}", flush=True)

                            # ── 累积到缓冲区（不再每个 chunk 直接 yield）──
                            # ofox 的 thinking 通过 reasoning_details list 返回：
                            #   reasoning_details=[{'index': 0, 'type': 'reasoning.text', 'text': '...'}, ...]
                            #   最后一个 chunk 会是 {'type': 'reasoning.encrypted', 'signature': '...'} —— 跳过（加密不可读）
                            rd_list = delta_obj.get("reasoning_details")
                            if isinstance(rd_list, list):
                                for rd_item in rd_list:
                                    if not isinstance(rd_item, dict):
                                        continue
                                    rd_type = rd_item.get("type", "")
                                    rd_text = rd_item.get("text", "")
                                    if rd_type == "reasoning.text" and rd_text:
                                        _thinking_buf += rd_text

                            # 兼容：OpenAI 原生协议有时把 thinking 放在 reasoning_content 里
                            if not isinstance(rd_list, list):
                                thinking = delta_obj.get("reasoning_content") or delta_obj.get("thinking")
                                if thinking:
                                    _thinking_buf += thinking

                            content = delta_obj.get("content")
                            if content:
                                _content_buf += content

                            # ── 达到 flush 间隔就吐出去（micro-batching 核心逻辑）──
                            _now = _time.time()
                            if _now - _last_flush_ts >= _FLUSH_INTERVAL:
                                async for item in _flush():
                                    yield item

                        # [DONE] 到达 → 把剩余缓冲区全吐出去
                        async for item in _flush():
                            yield item
                return  # 流式正常结束（[DONE] 到达）
            except _RETRYABLE_NETWORK_ERRORS as e:
                last_exc = e
                # 只在「连接建立阶段」（resp 还没拿到）重试
                if _stream_started or attempt >= self.MAX_RETRIES:
                    raise
                # HTTPStatusError 只对可恢复状态码重试（与非流式 _openai_chat 保持一致）
                if isinstance(e, httpx.HTTPStatusError):
                    if e.response is None or e.response.status_code not in self.RETRYABLE_STATUS:
                        raise
                wait = 0.5 * (attempt + 1)
                print(
                    f"[llm] ⚠️ 流式连接失败 (attempt {attempt + 1}/{self.MAX_RETRIES + 1}): "
                    f"{type(e).__name__}: {e}，{wait:.1f}s 后重试...",
                    flush=True,
                )
                await asyncio.sleep(wait)
        raise last_exc  # type: ignore[misc]
