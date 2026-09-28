"""New-API 中转网关客户端（OpenAI 协议）。

所有 LLM / VLM / 生图请求统一走 new-api，渠道分发由 new-api 后台按模型名配置，
业务层不感知也不干预。
"""

from __future__ import annotations

from wellflow.app.newapi.observability import model_call, event, response_received, request_prepared

import asyncio
import json
import time
from typing import Any

import httpx

from wellflow.app.llm.base import (
    BaseLLMClient,
    LLMResponse,
    InsufficientCreditsError,
    extract_error_message,
)
from wellflow.app.newapi.channel_audit import schedule_actual_channel
from wellflow.app.newapi.image_client import NewApiImageMixin

def _apply_reasoning_control(payload: dict[str, Any], model: str, reasoning_effort: str) -> None:
    """按模型族关闭/开启 thinking。

    - "close"：强制关闭（GLM/Qwen/豆包/DeepSeek 默认开推理会吞 completion 预算）
    - "low"/"medium"/"high"：透传 reasoning_effort + 对部分模型显式 enable_thinking=True
      （qwen 在只传 reasoning_effort 不强制 enable_thinking 时，有小概率把推理写进
      content 通道 —— 这就是 node1 的「thinking 泄漏」—— 所以非 close 模式下必须
      显式 enable_thinking=True 让模型把思考走独立 reasoning_content 通道）
    - None：禁止，调用方必须显式指定
    """
    if reasoning_effort is None:
        raise ValueError("reasoning_effort 不允许为 None，请显式传 'close' 或 'low'/'medium'/'high'")
    m = model.lower()
    if reasoning_effort != "close":
        payload["reasoning_effort"] = reasoning_effort
        # 🔴 关键：qwen / deepseek 在 effort=low 时若不显式 enable_thinking，
        # 部分版本会把推理写进 content 通道（"已识别品牌名称…" 这种动作进度句泄漏进报告正文）。
        # 显式 enable_thinking=True 确保模型走独立 reasoning_content 通道。
        if "qwen" in m:
            payload["enable_thinking"] = True
        elif "deepseek" in m:
            payload["enable_thinking"] = True
        elif "glm" in m or "doubao" in m or "seed" in m:
            payload["thinking"] = {"type": "enabled"}
        return
    # close 模式：各模型族用各自的关闭开关
    if "glm" in m or "doubao" in m or "seed" in m:
        payload["thinking"] = {"type": "disabled"}
    elif "qwen" in m:
        payload["enable_thinking"] = False
    elif "deepseek" in m:
        payload["enable_thinking"] = False
        payload["reasoning_effort"] = "none"
    # gemini 等其余模型不传字段即可

class NewApiGateway(NewApiImageMixin, BaseLLMClient):
    """纯 OpenAI 协议客户端，透传 extra_params 到 payload。"""

    MAX_RETRIES = 2
    RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(
        self,
        model: str = "qwen-max",
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        max_retries: int | None = None,
    ):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = self.MAX_RETRIES if max_retries is None else max_retries
        if self.max_retries < 0:
            raise ValueError("max_retries 不能小于 0")

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
        async for delta in self._openai_chat_stream(
            system=system, user=user, reasoning_effort=reasoning_effort,
            extra_params=extra_params, user_content=user_content,
            response_format=response_format,
        ):
            yield delta

    async def stream_chat(
        self,
        system: str,
        user: str,
        reasoning_effort: str = "close",
        response_format: dict[str, Any] | None = None,
        extra_params: dict[str, Any] | None = None,
    ):
        """纯文本流式 —— yield {"type": "thinking"|"content", "text": "..."}。

        和 stream_chat_with_images 共用同一条 SSE 解析路径，user_content=None
        时 _build_messages 会把 user 当作纯字符串消息内容。
        """
        async for delta in self._openai_chat_stream(
            system=system, user=user, reasoning_effort=reasoning_effort,
            extra_params=extra_params, user_content=None,
            response_format=response_format,
        ):
            yield delta

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

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

    @model_call("chat/completions")
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

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                # 非流式也去掉 timeout —— 上游推理慢（尤其带 thinking 的模型）时不再被 60s 截断
                async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                    request_prepared(payload["model"], "chat/completions", attempt + 1)
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers=self._build_headers(),
                        json=payload,
                    )
                    response_received(resp, payload["model"], "chat/completions")
                    schedule_actual_channel(resp, payload["model"], "chat/completions")
                    if resp.status_code in self.RETRYABLE_STATUS and attempt < self.max_retries:
                        event("重试", attempt=attempt + 1, next_attempt=attempt + 2, http_status=resp.status_code)
                        last_exc = httpx.HTTPStatusError(str(resp.status_code), request=resp.request, response=resp)
                        continue
                    if resp.status_code == 402:
                        raise InsufficientCreditsError(extract_error_message(402, resp.text))
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
                if attempt >= self.max_retries:
                    raise
                event("重试", attempt=attempt + 1, next_attempt=attempt + 2, error_type=type(e).__name__)
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # 流式 chat/completions (SSE)
    # ------------------------------------------------------------------

    @model_call("chat/completions:stream")
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
        for attempt in range(self.max_retries + 1):
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
                async with httpx.AsyncClient(timeout=None, trust_env=False) as client:

                    request_prepared(payload["model"], "chat/completions:stream", attempt + 1)
                    async with client.stream(
                        "POST", f"{self.base_url}/chat/completions",
                        headers=self._build_headers(accept_sse=True), json=payload,
                    ) as resp:
                        response_received(resp, payload["model"], "chat/completions:stream")
                        schedule_actual_channel(resp, payload["model"], "chat/completions:stream")
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
                if _stream_started or attempt >= self.max_retries:
                    raise
                if isinstance(e, httpx.HTTPStatusError):
                    if e.response is None or e.response.status_code not in self.RETRYABLE_STATUS:
                        raise
                wait = 0.5 * (attempt + 1)
                event("重试", attempt=attempt + 1, next_attempt=attempt + 2, error_type=type(e).__name__, wait_s=wait)
                await asyncio.sleep(wait)
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # LangChain 兼容层（Tool-Calling Agent 需要）
    # ------------------------------------------------------------------

    def langchain_compat(self):
        """返回一个 LangChain ChatModel 兼容对象。"""
        import httpx
        from langchain_openai import ChatOpenAI
        from openai import AsyncOpenAI, OpenAI

        # 显式提供 SDK 客户端，所有连接直连且不继承环境代理。
        connection = {
            "base_url": getattr(self, "base_url", None),
            "api_key": getattr(self, "api_key", "dummy"),
        }
        sync_client = OpenAI(**connection, http_client=httpx.Client(trust_env=False))
        async_client = AsyncOpenAI(**connection, http_client=httpx.AsyncClient(trust_env=False))
        return ChatOpenAI(
            client=sync_client.chat.completions,
            async_client=async_client.chat.completions,
            root_client=sync_client,
            root_async_client=async_client,
            model=getattr(self, "model", "qwen-max"),
            base_url=getattr(self, "base_url", None),
            api_key=getattr(self, "api_key", "dummy"),
            temperature=0.3,
        )
