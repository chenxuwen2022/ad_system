"""Node 1：ProductAnalyzer（VLM 商品识别 + 深度思考）。

必须走 VLM 识别商品图片，图片为必传参数，文字为可选补充。
直接返回 VLM 生成的原始文本报告，不做任何 schema 规整或字段限制。
开启 reasoning_effort 让 VLM 进行深度思考，提升商品识别报告的准确性。
"""

from __future__ import annotations

from wellflow.app.config import settings
from wellflow.app.llm.factory import get_llm_client
from wellflow.app.prompt.constant import PRODUCT_ANALYZER_SYSTEM_PROMPT


async def analyze_product(
    *,
    images: list[str],
    user_text: str = "",
    reasoning_effort: str | None = None,
) -> str:
    """调用 VLM 对商品图片做识别，返回原始文本报告。

    Args:
        images: 商品图片 data URI 列表，**必传**。空列表会直接抛 ValueError。
        user_text: 用户补充描述，可选。
        reasoning_effort: 推理/思考强度控制，可选值 "none" / "low" / "medium" / "high"。
            默认 None，读取 settings.llm_reasoning_effort（默认 "medium"）。

    Returns:
        VLM 按 PRODUCT_ANALYZER_SYSTEM_PROMPT 输出的完整文本报告，
        不做任何二次处理（无 JSON 解析、无 schema 规整）。
    """
    if not images:
        raise ValueError("ProductAnalyzer 必须传入至少一张商品图片")

    client = get_llm_client("vlm", node_name="node1")
    effort = reasoning_effort if reasoning_effort is not None else settings.llm_reasoning_effort

    user_message_parts: list[str] = []
    if user_text:
        user_message_parts.append(f"用户描述：{user_text}")
    user_message = "\n\n".join(user_message_parts)

    resp = await client.chat_with_images(
        system=PRODUCT_ANALYZER_SYSTEM_PROMPT,
        user=user_message,
        image_uris=images,
        reasoning_effort=effort,
        # 不传 response_format，让 VLM 自由输出文本（新 prompt 要求纯文本报告）
    )

    return resp.content


async def stream_analyze_product(
    *,
    images: list[str],
    user_text: str = "",
    reasoning_effort: str | None = None,
):
    """流式 VLM 商品识别，yield 每个 delta 文本片段。

    Args:
        images: 商品图片 data URI 列表，**必传**。
        user_text: 用户补充描述，可选。
        reasoning_effort: 推理强度控制。

    Yields:
        VLM 输出的文本 delta（str）。
    """
    if not images:
        raise ValueError("ProductAnalyzer 必须传入至少一张商品图片")

    client = get_llm_client("vlm", node_name="node1")
    effort = reasoning_effort if reasoning_effort is not None else settings.llm_reasoning_effort

    user_message_parts: list[str] = []
    if user_text:
        user_message_parts.append(f"用户描述：{user_text}")
    user_message_parts.append("请按要求输出完整的商品识别报告。")
    user_message = "\n\n".join(user_message_parts)

    async for delta in client.stream_chat_with_images(
        system=PRODUCT_ANALYZER_SYSTEM_PROMPT,
        user=user_message,
        image_uris=images,
        reasoning_effort=effort,
    ):
        if delta:
            yield delta
