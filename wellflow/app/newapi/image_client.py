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


# ---------------------------------------------------------------------------
# 尺寸适配（2026-09-29 双约束收敛）：
#   - Seedream / mai-image 等渠道硬下限 ≥ 2560×1440 = 3,686,400 像素；
#   - qwen-image-3.0 硬上限 ≤ 2048×2048 = 4,194,304 像素（超时报
#     "size is outside the model's pixel and aspect-ratio limits"）；
# 非 GPT Image 模型统一收敛到两模型共同合法区间 [3,686,400, 4,194,304]。
# ---------------------------------------------------------------------------
_SEEDREAM_MIN_PIXELS = 2560 * 1440
_QWEN_MAX_PIXELS = 2048 * 2048


def _is_qwen_image(model_name: str) -> bool:
    return "qwen-image" in (model_name or "").lower()


def _clamp_pixels(w: int, h: int, model_name: str) -> tuple[int, int]:
    """按模型约束对尺寸做比例保持的像素级收敛，并对齐到 8 的倍数（模型通用约束）。"""
    import math
    from wellflow.app.config import settings

    def _align8(v: int) -> int:
        return max(8, (v // 8) * 8)

    def _ratio_of(key: str) -> float:
        a, b = key.split(":")
        return float(a) / float(b)

    def _fallback_by_ratio(w0: int, h0: int, *, lo: int, hi: int) -> tuple[int, int]:
        """极端输入下的保底：按原始比例找 config 里最接近的合法档位。"""
        ratio = w0 / h0 if h0 else 1.0
        non_gpt_map = settings.image_ratio_to_pixel_size_non_gpt
        import re as _re
        for key in sorted(non_gpt_map.keys(), key=lambda k: abs(_ratio_of(k) - ratio)):
            mm = _re.match(r"(\d+)x(\d+)", non_gpt_map[key])
            if mm:
                fw, fh = int(mm.group(1)), int(mm.group(2))
                if lo <= fw * fh <= hi:
                    return fw, fh
        # 最后兜底：Seedream 下限档位
        return 2560, 1440

    def _rebalance(nw: int, nh: int, *, lo: int, hi: int) -> tuple[int, int]:
        """在保持 8 对齐前提下，把 (nw, nh) 推回 [lo, hi] 像素区间。"""
        nw, nh = _align8(nw), _align8(nh)
        for _ in range(20):
            px = nw * nh
            if lo <= px <= hi:
                return nw, nh
            if px > hi:
                if nw >= nh and nw > 8:
                    nw -= 8
                elif nh > 8:
                    nh -= 8
                else:
                    break
            else:  # px < lo
                if nw >= nh:
                    nw += 8
                else:
                    nh += 8
                nw, nh = _align8(nw), _align8(nh)
        # 迭代耗尽 → 按比例回退到 config 档位
        return _fallback_by_ratio(w, h, lo=lo, hi=hi)

    pixels = w * h
    if _is_qwen_image(model_name) and pixels > _QWEN_MAX_PIXELS:
        scale = math.sqrt(_QWEN_MAX_PIXELS / pixels)
        nw = max(8, int(w * scale))
        nh = max(8, int(h * scale))
        return _rebalance(nw, nh, lo=_SEEDREAM_MIN_PIXELS, hi=_QWEN_MAX_PIXELS)
    if pixels < _SEEDREAM_MIN_PIXELS:
        scale = math.sqrt(_SEEDREAM_MIN_PIXELS / pixels)
        nw = max(8, int(w * scale))
        nh = max(8, int(h * scale))
        return _rebalance(nw, nh, lo=_SEEDREAM_MIN_PIXELS, hi=_QWEN_MAX_PIXELS)
    return _align8(w), _align8(h)


def _remap_size_for_non_gpt(model_name: str, size: str) -> str:
    """非 GPT Image 模型的 size 自动适配（client 层单点兜底）：
    - 小于 Seedream 下限 → 按比例放大到最近合法档位；
    - 大于 qwen-image 上限 → 按比例缩小到 ≤ 2048×2048。
    """
    if "gpt-image" in (model_name or "").lower():
        return size  # GPT Image 保持原样

    import logging
    import re as _re

    m = _re.match(r"^(\d+)x(\d+)$", (size or "").strip())
    if not m:
        return size  # "2K" 这类枚举值原样透传

    w, h = int(m.group(1)), int(m.group(2))
    pixels = w * h

    # qwen-image 上限优先（超限必须收敛，否则直接被上游拒）
    if _is_qwen_image(model_name) and pixels > _QWEN_MAX_PIXELS:
        nw, nh = _clamp_pixels(w, h, model_name)
        logging.getLogger(__name__).warning(
            "[image_client] size clamped for qwen-image: %sx%s → %sx%s (pixels %d→%d)",
            w, h, nw, nh, pixels, nw * nh,
        )
        return f"{nw}x{nh}"

    if pixels >= _SEEDREAM_MIN_PIXELS:
        return size  # 两模型区间内，直接用

    # 小于 Seedream 下限 → 按比例匹配 settings.image_ratio_to_pixel_size_non_gpt
    from wellflow.app.config import settings
    ratio = w / h if h else 1.0
    non_gpt_map = settings.image_ratio_to_pixel_size_non_gpt

    def _ratio_of(key: str) -> float:
        a, b = key.split(":")
        return float(a) / float(b)

    best_key = min(non_gpt_map.keys(), key=lambda k: abs(_ratio_of(k) - ratio))
    upgraded = non_gpt_map[best_key]
    logging.getLogger(__name__).info(
        "[image_client] size remap: %s → %s (model=%s, reason=Ark/non-GPT below min pixels)",
        size, upgraded, model_name,
    )
    return upgraded


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

        # 构建 image_generation tool — 防御性 remap（responses 路径通常是 GPT Image 文生图，
        # 但如果将来渠道换为非 GPT，remap 会自动生效）
        image_tool_size = _remap_size_for_non_gpt(top_model, size)
        image_tool: dict[str, Any] = {
            "type": "image_generation",
            "size": image_tool_size,
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

        # 非 GPT 模型自动升级尺寸到 >= 2K 档位（避免 Ark Seedream 报像素不足）
        size = _remap_size_for_non_gpt(model_name, size)

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

        # 防御性 remap（edits 理论上只走 GPT Image，此处 pass-through 无副作用）
        size = _remap_size_for_non_gpt(model_name, size)

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

