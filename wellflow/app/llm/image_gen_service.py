"""共享生图服务：仅使用调用方指定的模型，保留并发限制和 429 重试。"""

from __future__ import annotations

from wellflow.app.newapi.observability import business_operation

from wellflow.app.llm.base import ImageGenResult, InsufficientCreditsError
from wellflow.app.llm.image_retry import generate_with_rate_limit_retry


@business_operation("生图")
async def generate_single_image(
    prompt: str,
    size: str,
    *,
    model: str,
    ref_data_uris: list[str] | None = None,
    log_id: str = "image-gen",
    task_id: str | None = None,
) -> ImageGenResult:
    """使用指定模型生成一张图；失败直接交给调用方处理，不切换模型。"""
    from wellflow.app.newapi.client_factory import get_llm_client

    model = model.strip()
    if not model:
        raise ValueError("缺少生图模型，请先选择生图模型")
    client = get_llm_client("image", model_override=model)
    result = await generate_with_rate_limit_retry(
        client, log_id=log_id, task_id=task_id,
        prompt=prompt, size=size, n=1, response_format="b64_json",
        extra_params={"image_refs": list(ref_data_uris or [])},
    )
    return result


def is_credits_error(exc: Exception) -> bool:
    return isinstance(exc, InsufficientCreditsError)
