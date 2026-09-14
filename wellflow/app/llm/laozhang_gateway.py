"""老张 AI 网关实现（Node3 图生图专用）。

接口特性（经实测）：
- 端点: POST {base_url}/v1/images/edits
- Content-Type: multipart/form-data
- 关键: 多个参考图通过**多个同名 image 字段**上传（curl -F image=@a.png -F image=@b.png -F image=@c.png）
- 响应: OpenAI 兼容格式 data[0].b64_json / data[0].url
- 不支持 /v1/responses 端点

LaOzhang 只适合 Node3 图生图场景，不支持 VLM/文本对话，chat* 方法抛 NotImplementedError。
"""

from __future__ import annotations

import asyncio
import base64
import re
from typing import Any

import httpx

from wellflow.app.llm.base import BaseLLMClient, ImageGenResult


class LaozhangGateway(BaseLLMClient):
    """老张 AI 图生图网关 — 只实现 generate_image，其他方法抛 NotImplementedError。"""

    MAX_RETRIES = 2
    RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    @staticmethod
    def _normalize_model(model: str) -> str:
        """老张 multipart 只认纯短名（如 gpt-image-2），不认 provider 前缀（如 openai/gpt-image-2）。

        自动剥掉第一个 "/" 之前的 provider 前缀，保留后面的短名。
        """
        if "/" in model:
            short = model.split("/", 1)[1]
            print(f"[laozhang] 🔧 模型名规范化: '{model}' → '{short}'（去掉 provider 前缀）", flush=True)
            return short
        return model

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "gpt-image-2",
        timeout: float = 180.0,
        proxy_url: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = self._normalize_model(model)
        self.timeout = timeout
        self.proxy_url = proxy_url

    # ------------------------------------------------------------------
    # VLM / 文本对话 — Laozhang 不支持
    # ------------------------------------------------------------------

    async def chat(self, *args, **kwargs):
        raise NotImplementedError("LaozhangGateway 只负责图生图")

    async def chat_with_images(self, *args, **kwargs):
        raise NotImplementedError("LaozhangGateway 只负责图生图")

    async def stream_chat_with_images(self, *args, **kwargs):
        raise NotImplementedError("LaozhangGateway 只负责图生图")

    # ------------------------------------------------------------------
    # generate_image — 直连 /v1/images/edits multipart（跳过 responses-first）
    # ------------------------------------------------------------------

    async def generate_image(
        self,
        prompt: str,
        *,
        image_uris: list[str] | None = None,
        size: str = "1536x1024",
        n: int = 1,
        response_format: str = "b64_json",
        extra_params: dict[str, Any] | None = None,
    ) -> ImageGenResult:
        """图生图 — multipart /v1/images/edits，所有参考图作为同名 image 字段上传。"""

        # 收集参考图
        refs: list[str] | None = list(image_uris) if image_uris else None
        if extra_params:
            for alias in ("image_refs", "image_uris", "reference_images"):
                if alias in extra_params:
                    val = extra_params[alias]
                    if val and not refs:
                        refs = list(val) if isinstance(val, list) else [val]

        # 组装 multipart data（文本字段）
        data: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "size": size,
            "quality": "high",
        }
        # Laozhang 不接受 response_format 参数 —— 实测返回总是 b64_json
        # 但如果调用方明确要 url，我们也传一下试试
        if response_format == "url":
            data["response_format"] = "url"
        if n > 1:
            data["n"] = n

        # 组装 multipart files —— 所有参考图作为同名 "image" 字段
        # [(field_name, (filename, bytes, content_type)), ...]
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        if refs:
            for idx, uri in enumerate(refs):
                mime, b64 = self._parse_data_uri(uri)
                ext = mime.split("/")[-1] or "png"
                filename = f"ref{idx + 1}.{ext}"
                raw_bytes = base64.b64decode(b64)
                files.append(("image", (filename, raw_bytes, mime)))
        else:
            # 无参考图时老张可能不接受 edits 端点 —— 退化为 generations
            # 但先按 edits 试试，不行再 fallback
            print(f"[laozhang] ⚠️ 无参考图，仍尝试 /images/edits", flush=True)

        headers = {"Authorization": f"Bearer {self.api_key}"}

        _prompt_preview = prompt[:120] + ("..." if len(prompt) > 120 else "")
        print(f"[laozhang] 📤 POST {self.base_url}/images/edits model={self.model} "
              f"refs={len(refs) if refs else 0} size={size} quality=high", flush=True)
        print(f"[laozhang]   prompt: {_prompt_preview}", flush=True)

        MAX_RETRIES = 2
        retryable = (httpx.ReadError, httpx.WriteError, httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)

        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 2):
            try:
                async with httpx.AsyncClient(timeout=self.timeout, proxy=self.proxy_url) as client:
                    resp = await client.post(
                        f"{self.base_url}/images/edits",
                        headers=headers,
                        data=data,
                        files=files if files else None,
                    )
                    resp.raise_for_status()
                    body = resp.json()
                break
            except retryable as exc:
                last_exc = exc
                if attempt <= MAX_RETRIES:
                    wait = 0.8 * attempt
                    print(f"[laozhang] ⚠️ 网络错误 (attempt {attempt}/{MAX_RETRIES + 1}): "
                          f"{type(exc).__name__}, {wait:.1f}s 后重试...", flush=True)
                    await asyncio.sleep(wait)
                    continue
                raise
            except httpx.HTTPStatusError as exc:
                if exc.response is not None:
                    body_preview = exc.response.text[:300]
                    print(f"[laozhang] ⚠️ HTTP {exc.response.status_code}: {body_preview}", flush=True)
                    if exc.response.status_code in self.RETRYABLE_STATUS and attempt <= MAX_RETRIES:
                        await asyncio.sleep(1.0 * attempt)
                        continue
                raise

        items = body.get("data") or []
        if not items:
            print(f"[laozhang] ❌ 响应无图: {json.dumps(body, indent=2, ensure_ascii=False)[:500]}", flush=True)
            return ImageGenResult(raw=body, model=body.get("model", self.model))

        model_label = body.get("model", self.model)
        variants: list[ImageGenResult] = []
        for it in items:
            variants.append(ImageGenResult(
                url=it.get("url"),
                b64_json=it.get("b64_json"),
                model=model_label,
            ))

        total_b64 = sum(len(v.b64_json or "") for v in variants)
        print(f"[laozhang] ✅ 生图成功 n={len(variants)} b64_total={total_b64} chars", flush=True)

        first = variants[0]
        return ImageGenResult(
            url=first.url,
            b64_json=first.b64_json,
            raw=body,
            model=model_label,
            variants=variants if len(variants) > 1 else None,
        )

    # ------------------------------------------------------------------
    # 辅助：data URI 解析
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_data_uri(data_uri: str) -> tuple[str, str]:
        """data:image/jpeg;base64,<b64> → ("image/jpeg", "<b64>")"""
        m = re.match(r"data:([^;]+);base64,(.+)", data_uri)
        if not m:
            raise ValueError(f"无效 data URI: {data_uri[:80]}...")
        return m.group(1), m.group(2)
