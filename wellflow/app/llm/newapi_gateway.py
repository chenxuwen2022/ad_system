"""New-API 中转网关客户端（OpenAI 协议）。

所有 LLM / VLM / 生图请求统一走 new-api，渠道分发由 new-api 后台按模型名配置，
业务层不感知也不干预。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from wellflow.app.llm.base import BaseLLMClient, LLMResponse


def _apply_reasoning_control(payload: dict[str, Any], model: str, reasoning_effort: str) -> None:
    """按模型族关闭/开启 thinking。

    - "close"：强制关闭（GLM/Qwen/豆包/DeepSeek 默认开推理会吞 completion 预算）
    - "low"/"medium"/"high"：透传 reasoning_effort
    - None：禁止，调用方必须显式指定
    """
    if reasoning_effort is None:
        raise ValueError("reasoning_effort 不允许为 None，请显式传 'close' 或 'low'/'medium'/'high'")
    if reasoning_effort != "close":
        payload["reasoning_effort"] = reasoning_effort
        return
    m = model.lower()
    if "glm" in m or "doubao" in m or "seed" in m:
        payload["thinking"] = {"type": "disabled"}
    elif "qwen" in m:
        payload["enable_thinking"] = False
    elif "deepseek" in m:
        payload["enable_thinking"] = False
        payload["reasoning_effort"] = "none"
    # gemini 等其余模型不传字段即可


class NewApiGateway(BaseLLMClient):
    """纯 OpenAI 协议客户端，透传 extra_params 到 payload。"""

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
        reasoning_effort: str,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.3,
        extra_params: dict[str, Any] | None = None,
        *,
        user_content: Any = None,
    ) -> LLMResponse:
        return await self._openai_chat(
            system=system, user=user, response_format=response_format,
            temperature=temperature, reasoning_effort=reasoning_effort,
            extra_params=extra_params, user_content=user_content,
        )

    async def chat_with_images(
        self,
        system: str,
        user: str,
        image_uris: list[str],
        reasoning_effort: str,
        response_format: dict[str, Any] | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> LLMResponse:
        user_content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for uri in image_uris:
            user_content.append({"type": "image_url", "image_url": {"url": uri}})
        self._log_payload_size(user_content)
        return await self._openai_chat(
            system=system, user=user, response_format=response_format,
            temperature=0.3, reasoning_effort=reasoning_effort,
            extra_params=extra_params, user_content=user_content,
        )

    async def stream_chat_with_images(
        self,
        system: str,
        user: str,
        image_uris: list[str],
        reasoning_effort: str,
        extra_params: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ):
        """yield {"type": "thinking"|"content", "text": "..."}。"""
        user_content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for uri in image_uris:
            user_content.append({"type": "image_url", "image_url": {"url": uri}})
        self._log_payload_size(user_content)
        async for delta in self._openai_chat_stream(
            system=system, user=user, reasoning_effort=reasoning_effort,
            extra_params=extra_params, user_content=user_content,
            response_format=response_format,
        ):
            yield delta

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _log_payload_size(self, user_content: Any) -> None:
        """多模态请求 payload 尺寸摘要，排查 image_url 截断用。"""
        if not isinstance(user_content, list):
            return
        image_blocks = [b for b in user_content if isinstance(b, dict) and b.get("type") == "image_url"]
        if not image_blocks:
            return
        total = 0
        for block in image_blocks:
            url = block.get("image_url", {}).get("url", "")
            comma_idx = url.find(",")
            total += len(url) - (comma_idx + 1) if comma_idx >= 0 else len(url)
        est_mb = total * 3 / 4 / 1024 / 1024
        print(f"[llm-payload] 📦 {len(image_blocks)} 张图, b64 合计 {total:,} chars (≈ {est_mb:.2f}MB)", flush=True)

    @staticmethod
    def _build_messages(system: str, user: str, user_content: Any) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
        msgs.append({"role": "user", "content": user_content if user_content is not None else user})
        return msgs

    def _build_headers(self, *, accept_sse: bool = False) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if accept_sse:
            h["Accept"] = "text/event-stream"
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _check_base_url(self) -> None:
        if not self.base_url:
            raise RuntimeError("LLM 网关 base_url 未配置，请在 .env 中设置 NEWAPI_API_KEY")

    # ------------------------------------------------------------------
    # 非流式 chat/completions
    # ------------------------------------------------------------------

    async def _openai_chat(
        self,
        system: str,
        user: str,
        response_format: dict[str, Any] | None,
        temperature: float,
        reasoning_effort: str,
        extra_params: dict[str, Any] | None,
        user_content: Any,
    ) -> LLMResponse:
        self._check_base_url()

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._build_messages(system, user, user_content),
            "temperature": temperature,
        }
        _apply_reasoning_control(payload, self.model, reasoning_effort)
        if response_format and response_format.get("type") == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if extra_params:
            payload.update(extra_params)

        print(f"[llm] POST {self.base_url}/chat/completions model={self.model} stream=False", flush=True)
        last_exc: Exception | None = None
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout, proxy=self.proxy_url, trust_env=False) as client:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers=self._build_headers(),
                        json=payload,
                    )
                    if resp.status_code in self.RETRYABLE_STATUS and attempt < self.MAX_RETRIES:
                        last_exc = httpx.HTTPStatusError(str(resp.status_code), request=resp.request, response=resp)
                        continue
                    if resp.status_code >= 400:
                        print(f"[llm] ❌ HTTP {resp.status_code} body={resp.text[:1000]}", flush=True)
                    resp.raise_for_status()
                    data = resp.json()

                msg = data.get("choices", [{}])[0].get("message", {})
                raw_content = msg.get("content")
                if raw_content is None:
                    parts = msg.get("parts")
                    if isinstance(parts, list):
                        raw_content = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))
                    elif isinstance(msg.get("content"), dict):
                        raw_content = msg["content"].get("text", "")
                    else:
                        raw_content = ""

                # thinking 提取（优先 reasoning_details，兼容 reasoning_content / thinking）
                thinking_text = None
                rd_list = msg.get("reasoning_details")
                if isinstance(rd_list, list):
                    parts = [rd["text"] for rd in rd_list if isinstance(rd, dict) and rd.get("type") == "reasoning.text" and rd.get("text")]
                    if parts:
                        thinking_text = "".join(parts)
                if not thinking_text:
                    thinking_text = msg.get("reasoning_content") or msg.get("thinking") or None

                return LLMResponse(
                    content=raw_content, raw=data, usage=data.get("usage", {}),
                    model=data.get("model", self.model), thinking=thinking_text,
                )
            except Exception as e:
                last_exc = e
                if attempt >= self.MAX_RETRIES:
                    raise
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # 流式 chat/completions (SSE)
    # ------------------------------------------------------------------

    async def _openai_chat_stream(
        self,
        system: str,
        user: str,
        reasoning_effort: str,
        extra_params: dict[str, Any] | None,
        user_content: Any,
        response_format: dict[str, Any] | None = None,
    ):
        """SSE 流式，micro-batching 20ms 合并窗口。"""
        _FLUSH_INTERVAL = 0.02
        self._check_base_url()

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._build_messages(system, user, user_content),
            "stream": True,
        }
        _apply_reasoning_control(payload, self.model, reasoning_effort)
        if response_format and response_format.get("type") == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if extra_params:
            payload.update(extra_params)

        retryable = (httpx.ReadError, httpx.WriteError, httpx.ConnectError, httpx.ConnectTimeout, httpx.HTTPStatusError)
        last_exc: Exception | None = None
        for attempt in range(self.MAX_RETRIES + 1):
            _stream_started = False
            _content_buf = ""
            _thinking_buf = ""
            _last_flush_ts = time.time()

            async def _flush():
                nonlocal _content_buf, _thinking_buf, _last_flush_ts
                if _thinking_buf:
                    yield {"type": "thinking", "text": _thinking_buf}
                    _thinking_buf = ""
                if _content_buf:
                    yield {"type": "content", "text": _content_buf}
                    _content_buf = ""
                _last_flush_ts = time.time()

            try:
                async with httpx.AsyncClient(timeout=None, proxy=self.proxy_url, trust_env=False) as client:
                    print(f"[llm] → POST {self.base_url}/chat/completions model={self.model} stream=True ...", flush=True)
                    async with client.stream(
                        "POST", f"{self.base_url}/chat/completions",
                        headers=self._build_headers(accept_sse=True), json=payload,
                    ) as resp:
                        resp.raise_for_status()
                        _stream_started = True
                        async for raw_line in resp.aiter_lines():
                            line = raw_line.strip()
                            if not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                            except Exception:
                                continue
                            choice0 = data.get("choices", [{}])[0] if data.get("choices") else {}
                            delta = choice0.get("delta", {})

                            # thinking（优先 reasoning_details，兼容 reasoning_content / thinking）
                            rd_list = delta.get("reasoning_details")
                            if isinstance(rd_list, list):
                                for rd in rd_list:
                                    if isinstance(rd, dict) and rd.get("type") == "reasoning.text" and rd.get("text"):
                                        _thinking_buf += rd["text"]
                            elif not isinstance(rd_list, list):
                                t = delta.get("reasoning_content") or delta.get("thinking")
                                if t:
                                    _thinking_buf += t

                            if delta.get("content"):
                                _content_buf += delta["content"]

                            if time.time() - _last_flush_ts >= _FLUSH_INTERVAL:
                                async for item in _flush():
                                    yield item

                        async for item in _flush():
                            yield item
                return
            except retryable as e:
                last_exc = e
                if _stream_started or attempt >= self.MAX_RETRIES:
                    raise
                if isinstance(e, httpx.HTTPStatusError):
                    if e.response is None or e.response.status_code not in self.RETRYABLE_STATUS:
                        raise
                wait = 0.5 * (attempt + 1)
                print(f"[llm] ⚠️ 流式连接失败 (attempt {attempt + 1}/{self.MAX_RETRIES + 1}): {type(e).__name__}，{wait:.1f}s 后重试", flush=True)
                await asyncio.sleep(wait)
        raise last_exc  # type: ignore[misc]
