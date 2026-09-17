"""Node 1：ProductAnalyzer（VLM 商品识别 + 深度思考）。

必须走 VLM 识别商品图片，图片为必传参数，文字为可选补充。
直接返回 VLM 生成的原始文本报告，不做任何 schema 规整或字段限制。
开启 reasoning_effort 让 VLM 进行深度思考，提升商品识别报告的准确性。
统一走 model_pool 轮询池，自动 failover + 熔断。
"""

from __future__ import annotations

from wellflow.app.config import settings
from wellflow.app.llm.model_pool import get_model_pool
from wellflow.app.prompt.constant import PRODUCT_ANALYZER_SYSTEM_PROMPT


async def analyze_product(
    *,
    images: list[str],
    user_text: str = "",
    reasoning_effort: str | None = None,
) -> str:
    """非流式 VLM 商品识别，返回原始文本报告。"""
    if not images:
        raise ValueError("ProductAnalyzer 必须传入至少一张商品图片")

    pool = get_model_pool()
    effort = reasoning_effort if reasoning_effort is not None else settings.llm_reasoning_effort

    user_text_parts: list[str] = []
    if user_text:
        user_text_parts.append(f"用户描述：{user_text}")
    user_text = "\n\n".join(user_text_parts)

    resp, used_model = await pool.chat_with_images(
        system=PRODUCT_ANALYZER_SYSTEM_PROMPT,
        user=user_text,
        image_uris=images,
        reasoning_effort=effort,
    )

    return resp.content


async def stream_analyze_product(
    *,
    images: list[str],
    user_text: str = "",
    reasoning_effort: str | None = None,
):
    """流式 VLM 商品识别，yield {"type": "thinking"|"content", "text": "..."}。"""
    if not images:
        raise ValueError("ProductAnalyzer 必须传入至少一张商品图片")

    pool = get_model_pool()
    effort = reasoning_effort if reasoning_effort is not None else settings.llm_reasoning_effort

    user_message_parts: list[str] = []
    if user_text:
        user_message_parts.append(f"用户描述：{user_text}")
    user_message_parts.append("请按要求输出完整的商品识别报告。")
    user_message = "\n\n".join(user_message_parts)

    async for delta in pool.stream_chat_with_images(
        system=PRODUCT_ANALYZER_SYSTEM_PROMPT,
        user=user_message,
        image_uris=images,
        reasoning_effort=effort,
    ):
        if delta:
            yield delta
