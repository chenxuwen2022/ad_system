"""New API 生图协议：generations、edits 和 responses。"""

from __future__ import annotations

from wellflow.app.newapi.observability import model_call, response_received, request_prepared

from typing import Any

from wellflow.app.llm.base import (
    ImageGenResult, ImageRateLimitError, InsufficientCreditsError, extract_error_message,
)
from wellflow.app.newapi.channel_audit import schedule_actual_channel

def gateway_request_context(response: Any) -> str:
    """保留可用于 New API 后台查实际渠道的请求 ID。"""
    request_id = response.headers.get("x-oneapi-request-id")
    return f" [request_id={request_id}]" if request_id else ""

class NewApiImageMixin:
    # ------------------------------------------------------------------
    # 图像生成 —— 按模型和参考图选择端点
    #   每张图一次调用；批量通过上层 node3 的并行池实现
    # ------------------------------------------------------------------

    @model_call("image")
    async def generate_image(
        self,
        prompt: str,
        *,
        image_uris: list[str] | None = None,  # 图生图：参考图 data URI 列表
        size: str = "1024x1536",               # 默认 3:4
        n: int = 1,                            # 批量由上层并行实现
        response_format: str = "b64_json",    # b64_json | url
        extra_params: dict[str, Any] | None = None,
    ) -> ImageGenResult:
        """图像生成 —— 按模型名 + 是否带参考图分流：

        - GPT Image 图生图（有参考图）→ /v1/images/edits multipart（默认），
          参考图作为多个同名 image 字段；可通过 image_gpt_edit_endpoint 切到 /v1/responses。
        - GPT Image 文生图（无参考图）→ /v1/images/generations JSON。
        - 非 GPT 模型（qwen/doubao 等）→ /v1/images/generations JSON，
          参考图通过 reference_images 字段传递。
        """
        import httpx
        from wellflow.app.config import settings

        refs: list[str] | None = list(image_uris) if image_uris else None
        if extra_params:
            for alias in ("image_refs", "image_uris", "reference_images"):
                if alias in extra_params:
                    val = extra_params[alias]
                    if val and not refs:
                        refs = list(val) if isinstance(val, list) else [val]

        # 非 GPT 模型或无参考图时，统一走 generations。
        model_name = getattr(self, "model", "")
        is_gpt_image = "gpt-image" in model_name.lower()
        if not is_gpt_image or not refs:
            return await self._generate_image_via_generations(
                prompt=prompt, refs=refs, size=size, n=n,
                response_format=response_format, extra_params=extra_params,
            )

        # GPT 图生图默认走 edits，只有显式配置 responses 时才继续向下执行。
        if getattr(settings, "image_gpt_edit_endpoint", "edits") != "responses":
            return await self._generate_image_via_edits(
                prompt=prompt, refs=refs, size=size, n=n,
                response_format=response_format,
            )

        top_model = settings.llm_model_responses.split("/", 1)[-1]

        # ---------- 从 config 读取速度/质量参数（调高质量→慢，调低→快）----------
        quality = settings.image_gen_quality
        in_fidelity = settings.image_gen_input_fidelity
        detail = settings.image_gen_detail

        # 构建 input content blocks
        content: list[dict[str, Any]] = [
            {"type": "input_text", "text": prompt},
        ]
        if refs:
            for uri in refs:
                content.append({"type": "input_image", "image_url": uri, "detail": detail})

        # 构建 image_generation tool
        image_tool: dict[str, Any] = {
            "type": "image_generation",
            "size": size,
            "quality": quality,
            "output_format": "png",
        }
        if refs:
            image_tool["action"] = "edit"
            image_tool["input_fidelity"] = in_fidelity  # 仅支持 high/low

        payload: dict[str, Any] = {
            "model": top_model,
            "input": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
            "tools": [image_tool],
            "tool_choice": {"type": "image_generation"},
        }

        api_key = getattr(self, "api_key", None)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        async with httpx.AsyncClient(timeout=settings.image_timeout, trust_env=False) as client:
            request_prepared(top_model, "responses")
            resp = await client.post(
                f"{self.base_url}/responses",
                headers=headers,
                json=payload,
            )
            response_received(resp, top_model, "responses")
            schedule_actual_channel(resp, top_model, "responses")

            if resp.status_code >= 400:
                context = gateway_request_context(resp)

                if resp.status_code == 429:
                    raise ImageRateLimitError(
                        extract_error_message(429, resp.text) + context,
                        resp.headers.get("Retry-After"),
                    )
                if resp.status_code == 402:
                    raise InsufficientCreditsError(extract_error_message(resp.status_code, resp.text) + context)
                raise RuntimeError(extract_error_message(resp.status_code, resp.text) + context)

            data = resp.json()

        # 解析 output 找 image_generation_call
        output = data.get("output") if isinstance(data, dict) else None
        if not isinstance(output, list):
            raise RuntimeError("/v1/responses 响应缺少 output 数组")

        for item in output:
            if item.get("type") == "image_generation_call":
                b64_result = item.get("result", "")
                if b64_result:
                    return ImageGenResult(
                        b64_json=b64_result,
                        raw=data,
                        model=data.get("model", top_model),
                    )

        raise RuntimeError(f"/v1/responses output 中未找到 image_generation_call")

    # ------------------------------------------------------------------
    # 图像生成 —— /v1/images/generations JSON body（非 GPT 模型用）
    #   qwen / doubao 等模型走这个路径
    #   参考图通过 reference_images 数组传递（data URI 格式）
    # ------------------------------------------------------------------

    async def _generate_image_via_generations(
        self,
        prompt: str,
        *,
        refs: list[str] | None = None,
        size: str = "1024x1536",
        n: int = 1,
        response_format: str = "b64_json",
        extra_params: dict[str, Any] | None = None,
    ) -> ImageGenResult:
        """非 GPT 模型生图 —— /v1/images/generations JSON body。"""
        import httpx

        from wellflow.app.config import settings

        model_name = getattr(self, "model", "")
        base_url = getattr(self, "base_url", "")
        api_key = getattr(self, "api_key", None)

        # 构建 payload
        payload: dict[str, Any] = {
            "model": model_name,
            "prompt": prompt,
            "size": size,
            "n": n,
            "response_format": response_format,
            "quality": "high",
        }

        # 参考图通过 reference_images 传递
        if refs:
            payload["reference_images"] = refs

        # 额外参数透传（如 negative_prompt 等）
        if extra_params:
            for k, v in extra_params.items():
                if k not in ("image_refs", "image_uris", "reference_images"):
                    payload[k] = v

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        async with httpx.AsyncClient(timeout=settings.image_timeout, trust_env=False) as client:
            request_prepared(payload["model"], "images/generations")
            resp = await client.post(
                f"{base_url}/images/generations",
                headers=headers,
                json=payload,
            )
            response_received(resp, model_name, "images/generations")
            schedule_actual_channel(resp, model_name, "images/generations")

            if resp.status_code >= 400:
                context = gateway_request_context(resp)
                if resp.status_code == 429:
                    raise ImageRateLimitError(
                        extract_error_message(429, resp.text) + context,
                        resp.headers.get("Retry-After"),
                    )
                if resp.status_code == 402:
                    raise InsufficientCreditsError(extract_error_message(resp.status_code, resp.text) + context)

                raise RuntimeError(extract_error_message(resp.status_code, resp.text) + context)

            data = resp.json()

        # 解析响应 —— OpenAI 兼容格式 data[].b64_json / data[].url
        items = data.get("data", []) if isinstance(data, dict) else []
        if not items:
            raise RuntimeError(f"/v1/images/generations 响应无图")

        model_label = data.get("model", model_name)
        variants: list[ImageGenResult] = []
        for it in items:
            b64 = it.get("b64_json")
            url = it.get("url")
            variants.append(ImageGenResult(url=url, b64_json=b64, model=model_label))

        # 主 result 取第一张 + variants 放全部（向后兼容）
        first = variants[0]
        return ImageGenResult(
            url=first.url,
            b64_json=first.b64_json,
            raw=data,
            model=model_label,
            variants=variants if len(variants) > 1 else None,
        )

    # ------------------------------------------------------------------
    # 图像生成 —— /v1/images/edits multipart（GPT Image 图生图专用）
    # ------------------------------------------------------------------

    async def _generate_image_via_edits(
        self,
        prompt: str,
        *,
        refs: list[str] | None = None,
        size: str = "1024x1536",
        n: int = 1,
        response_format: str = "b64_json",
    ) -> ImageGenResult:
        """GPT Image 图生图 —— multipart /v1/images/edits，参考图作为多个同名 image 字段。"""
        import base64
        import re
        import httpx

        from wellflow.app.config import settings

        base_url = getattr(self, "base_url", "")
        api_key = getattr(self, "api_key", None)
        model_name = getattr(self, "model", "")

        # 组装 multipart 文本字段（GPT Image 无 reference_images 字段）
        data: dict[str, Any] = {
            "model": model_name,
            "prompt": prompt,
            "size": size,
            "quality": settings.image_edit_quality,
        }
        if response_format == "url":
            data["response_format"] = "url"
        if n > 1:
            data["n"] = n

        # 参考图 data URI → bytes，作为多个同名「image」文件字段
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        if refs:
            for idx, uri in enumerate(refs):
                m = re.match(r"data:([^;]+);base64,(.+)", uri)
                if not m:
                    raise ValueError(f"无效 data URI: {uri[:80]}...")
                mime, b64 = m.group(1), m.group(2)
                ext = mime.split("/")[-1] or "png"
                raw_bytes = base64.b64decode(b64)
                files.append(("image", (f"ref{idx + 1}.{ext}", raw_bytes, mime)))

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

        async with httpx.AsyncClient(timeout=settings.image_timeout, trust_env=False) as client:
            request_prepared(model_name, "images/edits")
            resp = await client.post(
                f"{base_url}/images/edits",
                headers=headers,
                data=data,
                files=files if files else None,
            )
            response_received(resp, model_name, "images/edits")
            schedule_actual_channel(resp, model_name, "images/edits")
            if resp.status_code >= 400:
                context = gateway_request_context(resp)

                if resp.status_code == 429:
                    raise ImageRateLimitError(
                        extract_error_message(429, resp.text) + context,
                        resp.headers.get("Retry-After"),
                    )
                if resp.status_code == 402:
                    raise InsufficientCreditsError(extract_error_message(resp.status_code, resp.text) + context)
                raise RuntimeError(f"/v1/images/edits HTTP {resp.status_code}: {extract_error_message(resp.status_code, resp.text)}{context}")
            body = resp.json()

        items = (body or {}).get("data") or []
        if not items:
            raise RuntimeError("/v1/images/edits 响应无图")

        model_label = (body or {}).get("model", model_name)
        variants: list[ImageGenResult] = []
        for it in items:
            variants.append(ImageGenResult(
                url=it.get("url"),
                b64_json=it.get("b64_json"),
                model=model_label,
            ))

        first = variants[0]
        return ImageGenResult(
            url=first.url,
            b64_json=first.b64_json,
            raw=body,
            model=model_label,
            variants=variants if len(variants) > 1 else None,
        )

