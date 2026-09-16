"""Node 2：PlanningScheme — VLM 多模态一次产出 N 套结构化商拍方案。

输入：Node 1 的 product_insight（Markdown 报告）+ 产品图（data URI 列表）
      + 模特/参考图（data URI 列表，可选）+ 用户原始需求
      + scheme_count（要生成几套，默认 3）
输出：{"schemes": [scheme1, scheme2, scheme3]} — 每套是完整的 12 维 JSON 对象。

三套方案必须在方案定位、视觉主题、场景设定、模特气质、光影风格上有显著差异。
用 PLANNING_AGENT_SYSTEM_PROMPT（已改为输出 {"schemes": [...]} 数组格式）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from wellflow.app.llm.factory import get_llm_client
from wellflow.app.prompt.constant import PLANNING_AGENT_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 复用 JSON 提取（和旧 planning_agent 一致）
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict[str, Any]:
    """从 VLM 返回文本中提取 JSON。"""
    if not text:
        return {}

    try:
        obj = json.loads(text.strip())
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    fenced = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    fenced = re.sub(r"\s*```$", "", fenced)
    try:
        obj = json.loads(fenced.strip())
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    first = fenced.find("{")
    last = fenced.rfind("}")
    if first >= 0 and last > first:
        try:
            obj = json.loads(fenced[first:last + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    print(f"[planning_scheme] ⚠️ 无法解析 JSON, 原文本前200字: {text[:200]!r}", flush=True)
    return {}


# ---------------------------------------------------------------------------
# 流式
# ---------------------------------------------------------------------------


async def stream_plan_schemes(
    *,
    product_insight: str,
    product_images: list[str] | None = None,
    user_requirement: str = "",
    scheme_count: int = 3,
    reasoning_effort: str | None = None,
    model_images: list[str] | None = None,
):
    """流式生成 N 套风格迥异的商拍方案（仅基于商品识别报告文本，不传图片给 VLM）。"""
    from wellflow.app.config import settings

    client = get_llm_client("vlm", node_name="node2")
    effort = reasoning_effort if reasoning_effort is not None else settings.llm_reasoning_effort
    system_prompt = PLANNING_AGENT_SYSTEM_PROMPT

    # user message：基于商品识别报告文本 + 用户需求
    user_text_parts = [f"【商品识别报告】\n{product_insight}"]
    if user_requirement:
        user_text_parts.append(f"【用户创作需求】\n{user_requirement}")

    user_text_parts.append(
        f"请结合以上商品识别报告和用户创作需求，输出 **{scheme_count} 套风格迥异、场景互补** 的商拍方案。\n"
        f"三套方案在方案定位、视觉主题、场景设定、模特气质、光影风格上必须有显著差异，避免雷同。\n"
        f"⚠️ 方案中的 target_audience（目标客群）请根据商品定位合理推断，"
        f"model_profile（模特画像）请根据商品特性和目标客群设计合适的模特气质、年龄、性别。\n"
        f"输出格式严格按 System Prompt 中的 JSON schema，根节点为 {{\"schemes\": [...]}}，"
        f"schemes 数组长度 = {scheme_count}。"
    )

    print(f"[planning_scheme] 🎬 流式调用 VLM: scheme_count={scheme_count}, "
          f"reasoning_effort={effort} (不传图片)",
          flush=True)

    async for delta in client.stream_chat_with_images(
        system=system_prompt,
        user="\n\n".join(user_text_parts),
        image_uris=[],
        reasoning_effort=effort,
    ):
        if delta:
            yield delta


# ---------------------------------------------------------------------------
# 非流式（plan）
# ---------------------------------------------------------------------------


async def plan_schemes(
    *,
    product_insight: str,
    product_images: list[str] | None = None,
    user_requirement: str = "",
    scheme_count: int = 3,
    reasoning_effort: str | None = None,
    model_images: list[str] | None = None,
) -> dict[str, Any]:
    """非流式生成 N 套商拍方案（仅基于商品识别报告文本，不传图片给 VLM）。

    Returns:
        {"schemes": [scheme1_dict, scheme2_dict, ...], "raw_text": str, "thinking_text": str}
    """
    from wellflow.app.config import settings

    client = get_llm_client("vlm", node_name="node2")
    effort = reasoning_effort if reasoning_effort is not None else settings.llm_reasoning_effort
    system_prompt = PLANNING_AGENT_SYSTEM_PROMPT

    user_text_parts = [f"【商品识别报告】\n{product_insight}"]
    if user_requirement:
        user_text_parts.append(f"【用户创作需求】\n{user_requirement}")

    user_text_parts.append(
        f"请结合以上商品识别报告和用户创作需求，输出 **{scheme_count} 套风格迥异、场景互补** 的商拍方案。\n"
        f"三套方案在方案定位、视觉主题、场景设定、模特气质、光影风格上必须有显著差异，避免雷同。\n"
        f"⚠️ 方案中的 target_audience（目标客群）请根据商品定位合理推断，"
        f"model_profile（模特画像）请根据商品特性和目标客群设计合适的模特气质、年龄、性别。\n"
        f"输出格式严格按 System Prompt 中的 JSON schema，根节点为 {{\"schemes\": [...]}}，"
        f"schemes 数组长度 = {scheme_count}。"
    )

    print(f"[planning_scheme] 调用 VLM: scheme_count={scheme_count}, "
          f"reasoning_effort={effort} (不传图片)",
          flush=True)

    resp = await client.chat_with_images(
        system=system_prompt,
        user="\n\n".join(user_text_parts),
        image_uris=[],
        response_format={"type": "json_object"},
        reasoning_effort=effort,
    )

    raw = resp.content or ""
    thinking_text = getattr(resp, "thinking", None)
    print(f"[planning_scheme] VLM 返回原始文本 len={len(raw)}, "
          f"thinking={len(thinking_text) if thinking_text else 0}, 前100字={raw[:100]!r}", flush=True)

    parsed = _extract_json(raw)
    schemes = parsed.get("schemes", [])

    if not isinstance(schemes, list):
        print(f"[planning_scheme] ⚠️ schemes 不是 list，实际是 {type(schemes)}", flush=True)
        schemes = []

    # 兜底：按 scheme_count 截断 / 补空
    if len(schemes) > scheme_count:
        schemes = schemes[:scheme_count]
    elif len(schemes) < scheme_count and schemes:
        print(f"[planning_scheme] ⚠️ VLM 只返回 {len(schemes)}/{scheme_count} 套，补齐空方案", flush=True)
        schemes.extend([{"scheme_index": len(schemes), "scheme_name": "方案待补充", "_placeholder": True}] * (scheme_count - len(schemes)))

    print(f"[planning_scheme] ✅ 最终 schemes={len(schemes)} 套", flush=True)

    return {
        "schemes": schemes,
        "raw_text": raw,
        "thinking_text": thinking_text,
    }
