"""Node 2：PlanningAgent（VLM 多模态调用）。

输入：Node 1 的 product_insight（文字报告） + 产品图（data URI 列表） + 模特图（data URI 列表，可选）
      + count（要生成几个)
输出：{"generate_prompts": [prompt1, prompt2, ...]} —— JSON 结构，有 count 个元素

VLM 强制返回 JSON（response_format），不用正则解析。
"""

from __future__ import annotations

import json
import re
from typing import Any

from wellflow.app.llm.factory import get_llm_client
from wellflow.app.prompt.constant import PLANNING_AGENT_SYSTEM_PROMPT


def _extract_json(text: str) -> dict[str, Any]:
    """从 VLM 返回文本中提取 JSON。

    VLM 可能在 JSON 外包 ```json ... ``` fence，或者前后有些说明文字。
    """
    if not text:
        return {}

    # 先直接尝试
    try:
        obj = json.loads(text.strip())
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # 去掉 markdown code fence
    fenced = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    fenced = re.sub(r"\s*```$", "", fenced)
    try:
        obj = json.loads(fenced.strip())
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # 尝试找第一个 { 到最后一个 } 之间的内容
    first = fenced.find("{")
    last = fenced.rfind("}")
    if first >= 0 and last > first:
        try:
            obj = json.loads(fenced[first:last + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    print(f"[planning_agent] ⚠️ 无法解析 JSON, 原文本前200字: {text[:200]!r}", flush=True)
    return {}


async def stream_plan(
    *,
    product_insight: str,
    product_images: list[str],
    model_images: list[str] | None = None,
    count: int = 10,
    reasoning_effort: str | None = None,
):
    """流式 VLM 多模态生成 N 个服饰电商模特图提示词。

    与 plan() 的区别：
      - 用 stream_chat_with_images() 流式调用
      - 不传 response_format（让 VLM 自然输出 JSON，末尾再解析）
      - yield {"type": "thinking"|"content", "text": "..."} 每个 delta

    Args:
        product_insight: Node 1 输出的商品识别报告（纯文本）。
        product_images: 产品图 data URI 列表（必有）。
        model_images: 用户上传的模特图 data URI 列表（可选）。
        count: 要生成几个，前端 select 1-10，默认 10。
        reasoning_effort: 推理强度控制，默认读取 settings.llm_reasoning_effort。

    Yields:
        {"type": "thinking"|"content", "text": "..."} delta 片段。
        调用方负责收集完整 content 后用 _extract_json 解析。
    """
    from wellflow.app.config import settings

    if not product_images:
        raise ValueError("PlanningAgent 必须传入至少一张产品图")

    client = get_llm_client("vlm")
    effort = reasoning_effort if reasoning_effort is not None else settings.llm_reasoning_effort

    system_prompt = PLANNING_AGENT_SYSTEM_PROMPT

    # user message
    user_text_parts = [f"【识别报告】\n{product_insight}"]
    if model_images:
        user_text_parts.append("【图2】为用户指定模特图，请严格使用此模特特征。")
    else:
        user_text_parts.append("用户未上传模特图，请根据【识别报告】推荐模特。")
    user_text_parts.append(
        f"请结合【识别报告】与上方图片，按 V2 模板的 12 维策划逻辑进行思考，\n"
        f"最终以 JSON 格式输出恰好 {count} 个不同场景/角度的生图提示词数组，\n"
        f"格式: {{\"generate_prompts\": [\"第1个完整中文提示词\", \"第2个完整中文提示词\", ...]}}"
    )

    # 图片列表
    all_images = list(product_images)
    if model_images:
        all_images.extend(model_images)

    print(f"[planning_agent] 🎬 流式调用 VLM: count={count}, "
          f"product_images={len(product_images)}, model_images={len(model_images) if model_images else 0}, "
          f"reasoning_effort={effort}",
          flush=True)

    # 流式调用 —— 不传 response_format，让 VLM 自然输出 JSON
    async for delta in client.stream_chat_with_images(
        system=system_prompt,
        user="\n\n".join(user_text_parts),
        image_uris=all_images,
        reasoning_effort=effort,
    ):
        if delta:
            yield delta


async def plan(
    *,
    product_insight: str,
    product_images: list[str],
    model_images: list[str] | None = None,
    count: int = 10,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """VLM 多模态生成 N 个服饰电商模特图提示词（JSON 返回）。

    Args:
        product_insight: Node 1 输出的商品识别报告（纯文本）。
        product_images: 产品图 data URI 列表（必有）。
        model_images: 用户上传的模特图 data URI 列表（可选）。
        count: 要生成几个，前端 select 1-10，默认 10。
        reasoning_effort: 推理强度控制，默认读取 settings.llm_reasoning_effort。

    Returns:
        {"generate_prompts": [prompt1, prompt2, ...]}，长度 = count。
        如果 VLM 返回异常或 JSON 解析失败，generate_prompts 为空数组。
    """
    from wellflow.app.config import settings

    if not product_images:
        raise ValueError("PlanningAgent 必须传入至少一张产品图")

    client = get_llm_client("vlm")
    effort = reasoning_effort if reasoning_effort is not None else settings.llm_reasoning_effort

    system_prompt = PLANNING_AGENT_SYSTEM_PROMPT

    # user message
    user_text_parts = [f"【识别报告】\n{product_insight}"]
    if model_images:
        user_text_parts.append("【图2】为用户指定模特图，请严格使用此模特特征。")
    else:
        user_text_parts.append("用户未上传模特图，请根据【识别报告】推荐模特。")
    user_text_parts.append(
        f"请结合【识别报告】与上方图片，按 V2 模板的 12 维策划逻辑进行思考，\n"
        f"最终以 JSON 格式输出恰好 {count} 个不同场景/角度的生图提示词数组，\n"
        f"格式: {{\"generate_prompts\": [\"第1个完整中文提示词\", \"第2个完整中文提示词\", ...]}}"
    )

    # 图片列表
    all_images = list(product_images)
    if model_images:
        all_images.extend(model_images)

    print(f"[planning_agent] 调用 VLM: count={count}, "
          f"product_images={len(product_images)}, model_images={len(model_images) if model_images else 0}, "
          f"reasoning_effort={effort}",
          flush=True)

    resp = await client.chat_with_images(
        system=system_prompt,
        user="\n\n".join(user_text_parts),
        image_uris=all_images,
        response_format={"type": "json_object"},  # 强制 JSON
        reasoning_effort=effort,
    )

    raw = resp.content or ""
    thinking_text = getattr(resp, "thinking", None)
    print(f"[planning_agent] VLM 返回原始文本 len={len(raw)}, thinking={len(thinking_text) if thinking_text else 0}, 前100字={raw[:100]!r}", flush=True)

    parsed = _extract_json(raw)
    prompts = parsed.get("generate_prompts", [])

    if not isinstance(prompts, list):
        print(f"[planning_agent] ⚠️ generate_prompts 不是 list，实际是 {type(prompts)}", flush=True)
        prompts = []

    # 兜底：如果 VLM 返回的数量不对，按 count 截断或补空
    if len(prompts) > count:
        prompts = prompts[:count]
    elif len(prompts) < count and prompts:
        print(f"[planning_agent] ⚠️ VLM 只返回 {len(prompts)}/{count} 个，补齐空 prompt", flush=True)
        prompts.extend([""] * (count - len(prompts)))

    # 过滤掉空 prompt
    prompts = [p for p in prompts if p and str(p).strip()]

    print(f"[planning_agent] ✅ 最终 generate_prompts={len(prompts)} 个", flush=True)

    return {
        "generate_prompts": prompts,
        "raw_text": raw,  # 同时保留原始文本，方便 debug 或前端展示
        "thinking_text": thinking_text,  # 非流式 thinking（如果有）
    }
