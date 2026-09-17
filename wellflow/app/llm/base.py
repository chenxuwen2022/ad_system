"""LLM/VLM 网关抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LLMResponse:
    """统一的 LLM 响应。"""

    def __init__(
        self,
        content: str,
        raw: Any = None,
        usage: dict[str, int] | None = None,
        model: str = "",
        latency_ms: float | None = None,
        thinking: str | None = None,
    ):
        self.content = content
        self.raw = raw
        self.usage = usage or {}
        self.model = model
        self.latency_ms = latency_ms
        self.thinking = thinking  # 非流式响应中的推理/思考文本（如果有）


class ImageGenResult:
    """图像生成统一返回。

    单张: url / b64_json 直接赋值，variants=None
    批量（n>1）: variants 列表存每张子图，主 url/b64_json 取第一张（兼容旧代码）
    """

    def __init__(
        self,
        url: str | None = None,          # 远程 URL
        b64_json: str | None = None,     # base64 编码（不含 data: 前缀）
        raw: Any = None,
        model: str = "",
        variants: list["ImageGenResult"] | None = None,  # n>1 时存放所有子图
    ):
        self.url = url
        self.b64_json = b64_json
        self.raw = raw
        self.model = model
        self.variants = variants

    @property
    def data_uri(self) -> str | None:
        """如果有 b64_json，返回可直接用于前端 <img src> 的 data URI。"""
        if self.b64_json:
            return f"data:image/png;base64,{self.b64_json}"
        return None

    @property
    def all_images(self) -> list["ImageGenResult"]:
        """统一入口：返回本实例（单张）或 variants（批量）。"""
        if self.variants:
            return self.variants
        return [self]


def _extract_error_message(status_code: int, text: str) -> str:
    """从网关错误响应里提取干净的 message，供前端原封不动展示。

    常见结构：{"error":{"message":"...","type":"..."}} 或 {"detail":"..."}。
    解析失败就回退到原始文本。
    """
    import json

    raw = (text or "").strip()
    try:
        body = json.loads(raw)
    except Exception:
        return raw or f"HTTP {status_code}"

    err = body.get("error")
    if isinstance(err, dict):
        for key in ("message", "detail", "msg", "title"):
            val = err.get(key)
            if val:
                return str(val)

    for key in ("message", "detail", "msg"):
        val = body.get(key)
        if val:
            return str(val)

    return raw or f"HTTP {status_code}"


class BaseLLMClient(ABC):
    """统一的 LLM 客户端接口。所有网关实现必须遵守这个契约。"""

    @abstractmethod
    async def chat(
        self,
        system: str,
        user: str,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.3,
        reasoning_effort: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """纯文本/结构化输出。

        Args:
            reasoning_effort: 推理/思考强度控制，可选值 "none" / "low" / "medium" / "high"。
                None 表示使用模型默认行为。
            extra_params: 透传到 payload 的额外扩展字段，供网关识别模型特定参数。
        """
        ...

    @abstractmethod
    async def chat_with_images(
        self,
        system: str,
        user: str,
        image_uris: list[str],
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """多模态 VLM 调用（完整响应）。"""
        ...

    @abstractmethod
    async def stream_chat_with_images(
        self,
        system: str,
        user: str,
        image_uris: list[str],
        reasoning_effort: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> Any:
        """多模态 VLM 流式调用。

        Returns:
            异步迭代器，每次 yield 一个 str（delta 文本片段）。
        """
        ...

    # ------------------------------------------------------------------
    # 图像生成 —— 只走 /v1/responses 端点
    #   支持多参考图（input_image blocks）、高清编辑、统一协议
    #   每张图一次调用；批量通过上层 node3 的并行池实现
    # ------------------------------------------------------------------

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
        - GPT Image 文生图（无参考图）→ /v1/responses + image_generation tool。
        - 非 GPT 模型（qwen/doubao 等）→ /v1/images/generations JSON，
          参考图通过 reference_images 字段传递。
        """
        import httpx, asyncio as _asyncio
        from wellflow.app.config import settings

        refs: list[str] | None = list(image_uris) if image_uris else None
        if extra_params:
            for alias in ("image_refs", "image_uris", "reference_images"):
                if alias in extra_params:
                    val = extra_params[alias]
                    if val and not refs:
                        refs = list(val) if isinstance(val, list) else [val]

        # ---------- 🔑 按模型名分流 ----------
        model_name = getattr(self, "model", "")
        is_gpt_image = "gpt-image" in model_name.lower()

        # GPT edit 模式端点选择（默认 "edits"，可配成 "responses"）
        gpt_edit_endpoint = getattr(settings, "image_gpt_edit_endpoint", "edits")

        # ═══════════════════════════════════════════════════════════════════════════
        # 分流规则：**全部直接用用户指定的模型**，不再换模型名
        #
        #  ① GPT + 有 refs + 默认配置       → /v1/images/edits multipart
        #                                      gpt-image-2 原生 edit 端点，
        #                                      参考图作为多个同名 image 文件字段
        #
        #  ② GPT + 无 refs（纯文生图）      → /v1/images/generations JSON
        #                                      gpt-image-2 原生 generations，
        #                                      不需要 gpt-5.4-mini 当大脑
        #
        #  ③ 非 GPT 模型（qwen/doubao 等） → /v1/images/generations JSON
        #                                      参考图通过 reference_images 字段传递
        #
        #  ④ GPT edit + 强制 responses     → /v1/responses + image_generation
        #     （image_gpt_edit_endpoint    tool (action=edit)
        #       = "responses" 时）           ⚠️ 仅这个分支需要 llm_model_responses
        # ═══════════════════════════════════════════════════════════════════════════

        # ── ① GPT 图生图（有 refs）→ edits multipart（默认路径）──
        if is_gpt_image and refs and gpt_edit_endpoint != "responses":
            return await self._generate_image_via_edits(
                prompt=prompt, refs=refs, size=size, n=n,
                response_format=response_format,
            )

        # ── ④ GPT 图生图 + 强制 responses → responses + image_generation tool ──
        if is_gpt_image and refs and gpt_edit_endpoint == "responses":
            # 只有这个分支需要额外的 responses 端点配置
            from wellflow.app.llm.factory import _strip_provider
            top_model = _strip_provider(getattr(settings, "llm_model_responses", "openai/gpt-5.4-mini"))
        else:
            # ── ② + ③ GPT 纯文生图 / 非 GPT / 其他 → generations JSON ──
            return await self._generate_image_via_generations(
                prompt=prompt, refs=refs, size=size, n=n,
                response_format=response_format, extra_params=extra_params,
            )

        # ---------- 从 config 读取速度/质量参数（调高质量→慢，调低→快）----------
        quality = settings.image_gen_quality
        in_fidelity = settings.image_gen_input_fidelity
        detail = settings.image_gen_detail
        # /v1/responses 单独的代理开关：默认 None=直连，跳过 HTTP 代理
        proxy = settings.image_gen_proxy_url

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
            image_tool["input_fidelity"] = in_fidelity  # ofox 只支持 high/low

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

        is_edit = image_tool.get("action") == "edit"
        _prompt_preview = prompt[:120] + ("..." if len(prompt) > 120 else "")
        print(f"[llm] 🆕 POST /v1/responses model={top_model} "
              f"{'(edit)' if is_edit else '(generate)'} refs={len(refs) if refs else 0} "
              f"size={size} quality={quality} fidelity={in_fidelity} detail={detail} "
              f"proxy={proxy or '(直连)'}", flush=True)
        print(f"[llm]   prompt: {_prompt_preview}", flush=True)

        MAX_RETRIES = 2
        retryable = (httpx.ReadError, httpx.WriteError, httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)

        # ---------- AsyncClient 建在循环外，重试时复用连接池 ----------
        import json as _json
        _payload_size = len(_json.dumps(payload).encode())
        _n_refs = len(payload.get("input", [{}])[0].get("content", [])) - 1  # 减 1 是 input_text
        print(f"[llm] 📦 payload ≈ {_payload_size/1024:.0f}KB ({_n_refs} refs, {payload['tools'][0].get('quality','?')} quality)", flush=True)

        async with httpx.AsyncClient(timeout=settings.image_timeout, proxy=proxy) as client:
            last_exc: Exception | None = None
            data = None
            for attempt in range(1, MAX_RETRIES + 2):
                try:
                    _t_req = _asyncio.get_event_loop().time()
                    resp = await client.post(
                        f"{self.base_url}/responses",
                        headers=headers,
                        json=payload,
                    )
                    _t_resp = _asyncio.get_event_loop().time()
                    _http_dt = _t_resp - _t_req
                    # 服务端处理时间（从发起到收到完整响应）
                    print(f"[llm] ⏱️ HTTP round-trip {_http_dt:.1f}s "
                          f"(status={resp.status_code}, attempt={attempt})", flush=True)
                    if resp.status_code >= 400:
                        err_body = resp.text[:500]
                        print(f"[llm] /v1/responses HTTP {resp.status_code}: {err_body}", flush=True)
                        if resp.status_code in (400, 401, 403, 404):
                            raise RuntimeError(_extract_error_message(resp.status_code, resp.text))
                        if resp.status_code in (429, 500, 502, 503, 504) and attempt <= MAX_RETRIES:
                            await _asyncio.sleep(1.0 * attempt)
                            continue
                        raise RuntimeError(_extract_error_message(resp.status_code, resp.text))

                    data = resp.json()
                    break
                except retryable as exc:
                    last_exc = exc
                    if attempt <= MAX_RETRIES:
                        wait = 0.8 * attempt
                        print(f"[llm] ⚠️ /v1/responses 网络错误 (attempt {attempt}/{MAX_RETRIES+1}): {type(exc).__name__}, {wait:.1f}s 后重试...", flush=True)
                        await _asyncio.sleep(wait)
                        continue
                    raise

        # 解析 output 找 image_generation_call
        output = data.get("output") if isinstance(data, dict) else None
        if not isinstance(output, list):
            print(f"[llm] ⚠️ /v1/responses 响应缺少 output 数组", flush=True)
            raise RuntimeError("/v1/responses 响应缺少 output 数组")

        for item in output:
            if item.get("type") == "image_generation_call":
                b64_result = item.get("result", "")
                revised_prompt = item.get("revised_prompt", "")
                if b64_result:
                    print(f"[llm] ✅ /v1/responses 生图成功 b64={len(b64_result)} chars "
                          f"revised_prompt={revised_prompt[:100]}", flush=True)
                    return ImageGenResult(
                        b64_json=b64_result,
                        raw=data,
                        model=data.get("model", top_model),
                    )

        raise RuntimeError(f"/v1/responses output 中未找到 image_generation_call, raw={str(data)[:300]}")

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
        """非 GPT 模型 / ofox 网关生图 —— /v1/images/generations JSON body。"""
        import json, httpx, asyncio as _asyncio

        from wellflow.app.config import settings

        model_name = getattr(self, "model", "")
        base_url = getattr(self, "base_url", "")
        api_key = getattr(self, "api_key", None)
        # 🔑 代理策略：与 /v1/responses 路径保持一致，用 image_gen_proxy_url（默认 None=直连）
        proxy = settings.image_gen_proxy_url

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

        _prompt_preview = prompt[:120] + ("..." if len(prompt) > 120 else "")
        _payload_size = len(json.dumps(payload).encode())
        print(f"[llm-generations] 📤 POST {base_url}/images/generations "
              f"model={model_name} refs={len(refs) if refs else 0} "
              f"size={size} payload≈{_payload_size/1024:.0f}KB", flush=True)
        print(f"[llm-generations]   prompt: {_prompt_preview}", flush=True)

        MAX_RETRIES = 2
        retryable = (httpx.ReadError, httpx.WriteError, httpx.ConnectError,
                     httpx.ConnectTimeout, httpx.ReadTimeout)

        async with httpx.AsyncClient(timeout=settings.image_timeout, proxy=proxy) as client:
            last_exc: Exception | None = None
            data = None
            for attempt in range(1, MAX_RETRIES + 2):
                try:
                    _t0 = _asyncio.get_event_loop().time()
                    resp = await client.post(
                        f"{base_url}/images/generations",
                        headers=headers,
                        json=payload,
                    )
                    _t1 = _asyncio.get_event_loop().time()
                    print(f"[llm-generations] ⏱️ HTTP {resp.status_code} round-trip {_t1 - _t0:.1f}s "
                          f"(attempt={attempt})", flush=True)

                    if resp.status_code >= 400:
                        err_body = resp.text[:500]
                        print(f"[llm-generations] ❌ HTTP {resp.status_code}: {err_body}", flush=True)
                        if resp.status_code in (400, 401, 403, 404):
                            raise RuntimeError(_extract_error_message(resp.status_code, resp.text))
                        if resp.status_code in (429, 500, 502, 503, 504) and attempt <= MAX_RETRIES:
                            await _asyncio.sleep(1.0 * attempt)
                            continue
                        raise RuntimeError(_extract_error_message(resp.status_code, resp.text))

                    data = resp.json()
                    break
                except retryable as exc:
                    last_exc = exc
                    if attempt <= MAX_RETRIES:
                        wait = 0.8 * attempt
                        print(f"[llm-generations] ⚠️ 网络错误 (attempt {attempt}/{MAX_RETRIES+1}): "
                              f"{type(exc).__name__}, {wait:.1f}s 后重试...", flush=True)
                        await _asyncio.sleep(wait)
                        continue
                    raise

        # 解析响应 —— OpenAI 兼容格式 data[].b64_json / data[].url
        items = data.get("data", []) if isinstance(data, dict) else []
        if not items:
            print(f"[llm-generations] ❌ 响应无图: {json.dumps(data, indent=2, ensure_ascii=False)[:500]}", flush=True)
            raise RuntimeError(f"/v1/images/generations 响应无图, raw={str(data)[:300]}")

        model_label = data.get("model", model_name)
        variants: list[ImageGenResult] = []
        for it in items:
            b64 = it.get("b64_json")
            url = it.get("url")
            variants.append(ImageGenResult(url=url, b64_json=b64, model=model_label))

        # 日志
        total_b64 = sum(len(v.b64_json or "") for v in variants)
        print(f"[llm-generations] ✅ 生图成功 n={len(variants)} "
              f"b64_total={total_b64} chars", flush=True)

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
    #   gpt-image-2 原生编辑端点：参考图作为多个同名「image」文件字段上传，
    #   不认 JSON reference_images。与 LaozhangGateway 的 multipart 模式一致。
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
        import json
        import re
        import httpx
        import asyncio as _asyncio

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

        _prompt_preview = prompt[:120] + ("..." if len(prompt) > 120 else "")
        print(f"[llm-edits] 📤 POST {base_url}/images/edits model={model_name} "
              f"refs={len(refs) if refs else 0} size={size} quality=high n={n}", flush=True)
        print(f"[llm-edits]   prompt: {_prompt_preview}", flush=True)

        MAX_RETRIES = 2
        retryable = (httpx.ReadError, httpx.WriteError, httpx.ConnectError,
                     httpx.ConnectTimeout, httpx.ReadTimeout)
        RETRYABLE_STATUS = {429, 500, 502, 503, 504}

        body: dict[str, Any] | None = None
        async with httpx.AsyncClient(timeout=settings.image_timeout,
                                     proxy=settings.image_gen_proxy_url) as client:
            for attempt in range(1, MAX_RETRIES + 2):
                try:
                    resp = await client.post(
                        f"{base_url}/images/edits",
                        headers=headers,
                        data=data,
                        files=files if files else None,
                    )
                    if resp.status_code >= 400:
                        err_body = resp.text[:500]
                        print(f"[llm-edits] ❌ HTTP {resp.status_code}: {err_body}", flush=True)
                        if resp.status_code in RETRYABLE_STATUS and attempt <= MAX_RETRIES:
                            await _asyncio.sleep(1.0 * attempt)
                            continue
                        raise RuntimeError(f"/v1/images/edits HTTP {resp.status_code}: {err_body}")
                    body = resp.json()
                    break
                except retryable as exc:
                    if attempt <= MAX_RETRIES:
                        print(f"[llm-edits] ⚠️ 网络错误 (attempt {attempt}/{MAX_RETRIES + 1}): "
                              f"{type(exc).__name__}, 重试...", flush=True)
                        await _asyncio.sleep(0.8 * attempt)
                        continue
                    raise

        items = (body or {}).get("data") or []
        if not items:
            raise RuntimeError(f"/v1/images/edits 响应无图, raw={json.dumps(body, ensure_ascii=False)[:300]}")

        model_label = (body or {}).get("model", model_name)
        variants: list[ImageGenResult] = []
        for it in items:
            variants.append(ImageGenResult(
                url=it.get("url"),
                b64_json=it.get("b64_json"),
                model=model_label,
            ))

        total_b64 = sum(len(v.b64_json or "") for v in variants)
        print(f"[llm-edits] ✅ 生图成功 n={len(variants)} b64_total={total_b64} chars", flush=True)

        first = variants[0]
        return ImageGenResult(
            url=first.url,
            b64_json=first.b64_json,
            raw=body,
            model=model_label,
            variants=variants if len(variants) > 1 else None,
        )

    # ------------------------------------------------------------------
    # LangChain 兼容层（Tool-Calling Agent 需要）
    # ------------------------------------------------------------------

    def langchain_compat(self):
        """返回一个 LangChain ChatModel 兼容对象。"""
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=getattr(self, "model", "qwen-max"),
            base_url=getattr(self, "base_url", None),
            api_key=getattr(self, "api_key", "dummy"),
            temperature=0.3,
        )
