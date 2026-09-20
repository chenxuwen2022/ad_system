"""Node 2：PlanningScheme — VLM 多模态产出 1 套结构化商拍方案。

输入：Node 1 的 product_insight（Markdown 报告）+ 产品图（data URI 列表）
      + 用户原始需求
输出：{"schemes": [scheme1]} — 1 套完整的 12 维 JSON 对象。

⚠️ Node2 **不使用模特图**：模特图只在 Node3 prompt 生成阶段才喂给 VLM，
   让 Node3 在最终生图提示词里严格对齐模特的人脸/体型/气质。
   Node2 只负责基于商品和用户需求产出最终商拍方案。

用 PLANNING_AGENT_SYSTEM_PROMPT（输出 {"schemes": [...]} 数组格式）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from wellflow.app.llm.model_pool import get_model_pool
from wellflow.app.prompt.constant import PLANNING_AGENT_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 复用 JSON 提取（和旧 planning_agent 一致）
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict[str, Any]:
    """从 VLM 返回文本中提取 JSON。

    四层兜底，按降级顺序尝试：
      1. json.loads 直接解析（最理想）
      2. 剥 code fence 后 json.loads
      3. 首尾花括号切片后 json.loads
      4. ❗ 容错修复后 json.loads —— 修 trailing comma、补缺失括号

    如果四层都失败，尝试正则抽 scheme_index 做降级方案（至少能拿到方案名数量）。

    关键洞察：Node2 流式路径传了 response_format=json_object 后，
    LLM 输出应该是严格合法 JSON。如果仍失败，多半是流式 chunk 拼接
    时尾部被截断或 thinking 混入——此时容错修复能解决 80% 的情况。
    """
    if not text:
        return {}

    steps = [
        ("直接", lambda t: t.strip()),
        ("剥 code fence", lambda t: re.sub(r"\s*$", "",
                        re.sub(r"^```(?:json)?\s*", "", t.strip(), flags=re.IGNORECASE))),
        ("首尾花括号切片", lambda t: t.strip()[
            t.find("{"): t.rfind("}") + 1] if t.strip().find("{") >= 0 and t.strip().rfind("}") > t.strip().find("{") else t),
    ]

    for step_name, transform in steps:
        try:
            obj = json.loads(transform(text))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue

    # ── 第四步：容错修复 ──
    candidates = []
    for _, transform in steps:
        t = transform(text)
        candidates.append(t)
    candidates.sort(key=len, reverse=True)
    for cand in candidates:
        try:
            fixed = _repair_json(cand)
            obj = json.loads(fixed)
            if isinstance(obj, dict):
                print(f"[planning_scheme] 🔧 JSON 容错修复成功", flush=True)
                return obj
        except json.JSONDecodeError:
            continue

    # ── 第五步：正则降级抽 scheme_index / scheme_name ──
    print(f"[planning_scheme] ⚠️ JSON 解析全失败，尝试正则降级抽取", flush=True)
    return _regex_extract_schemes(text)


def _regex_extract_schemes(text: str) -> dict[str, Any]:
    """正则降级：从 LLM 输出文本中提取出 scheme_index / scheme_name 对。

    当完整 JSON 无法解析时，至少能拿到方案数量和方案名，
    让后续 `len(schemes) > 0` 检查不被误触发（不会 raise RuntimeError）。
    这种降级方案会缺失完整的 12 维字段，但比全空好——
    前端至少能显示 "方案待补充" 的占位条目。
    """
    # 抽 scheme_index + scheme_name 对
    pattern = r'"scheme_index"\s*:\s*(\d+)[^}]*?"scheme_name"\s*:\s*"([^"]*)"'
    matches = re.findall(pattern, text, re.DOTALL)

    # 或者反过来先抽 name 再抽 index
    if not matches:
        pattern2 = r'"scheme_name"\s*:\s*"([^"]*)"[^}]*?"scheme_index"\s*:\s*(\d+)'
        matches = [(int(idx), name) for name, idx in re.findall(pattern2, text, re.DOTALL)]

    if not matches:
        print(f"[planning_scheme] 🔍 正则也没抽到任何 scheme_index/scheme_name → schemes=[]", flush=True)
        return {}

    # 按 index 排序，构造简化的 scheme dict
    matches.sort(key=lambda x: int(x[0]))
    schemes = [
        {
            "scheme_index": int(idx),
            "scheme_name": name or f"方案 {idx + 1}",
            "_placeholder": True,
            "_extracted_by_regex": True,
        }
        for idx, name in matches
    ]
    print(f"[planning_scheme] 🔧 正则降级成功：从 len={len(text)} 的文本中抽到 {len(schemes)} 个方案", flush=True)
    return {"schemes": schemes}


def _repair_json(text: str) -> str:
    """修复 JSON 常见小语法错误：
      1. trailing comma —— 数组/对象最后一个元素后面的多余逗号
      2. 截断 JSON —— 尾部缺失闭合括号/引号，尝试补齐
    """
    t = text.strip()

    # 修 trailing comma —— ",}" 和 ",]"
    t = re.sub(r",\s*([}\]])", r"\1", t)

    # 修截断 JSON —— 尾部缺失闭合字符
    t = _close_braces(t)

    return t


def _close_braces(text: str) -> str:
    """尝试补齐缺失的闭合括号，让截断的 JSON 能被 json.loads 接受。

    简化策略：只统计顶层花括号/方括号的未闭合数，不处理嵌套字符串里的花括号。
    """
    s = text
    n = len(s)
    if n == 0:
        return s

    opens = {"{": 0, "[": 0}
    for ch in s:
        if ch == "{": opens["{"] += 1
        elif ch == "}": opens["{"] -= 1
        elif ch == "[": opens["["] += 1
        elif ch == "]": opens["["] -= 1

    if opens["{"] <= 0 and opens["["] <= 0:
        return s  # 无缺失

    # 补闭合符：先补内层数组再补外层对象
    closers = []
    for _ in range(opens["["]):
        closers.append("]")
    for _ in range(opens["{"]):
        closers.append("}")
    s = s.rstrip() + "".join(closers)

    return s


# ---------------------------------------------------------------------------
# 流式（plan） — 复用 plan_schemes 的 user message 构造
# ---------------------------------------------------------------------------


async def stream_plan_schemes(
    *,
    product_insight: str,
    product_images: list[str] | None = None,
    user_requirement: str = "",
    scheme_count: int = 3,
    reasoning_effort: str | None = None,
):
    """流式生成 3 套方案，yield {"type": "thinking"|"content", "text": "..."}。

    ⚠️ Node2 **不使用模特图**：模特图只在 Node3 prompt 生成阶段才喂给 VLM。
    """
    from wellflow.app.config import settings

    pool = get_model_pool()
    effort = reasoning_effort if reasoning_effort is not None else settings.llm_reasoning_effort
    system_prompt = PLANNING_AGENT_SYSTEM_PROMPT

    product_images = product_images or []
    n_prod = len(product_images)

    user_text_parts = [f"【商品识别报告】\n{product_insight}"]
    if user_requirement:
        user_text_parts.append(f"【用户创作需求】\n{user_requirement}")

    if n_prod:
        user_text_parts.append(
            "【参考图编号说明】\n"
            f"共 {n_prod} 张参考图（商品图：服装外观、颜色、细节、材质），编号为 1 ~ {n_prod}。"
        )

    user_text_parts.append(
        f"请结合以上商品识别报告和用户创作需求，输出 **{scheme_count} 套风格迥异、场景互补** 的商拍方案。\n"
        f"三套方案在方案定位、视觉主题、场景设定、模特气质、光影风格上必须有显著差异，避免雷同。\n"
        f"⚠️ 方案中的 target_audience（目标客群）请根据商品定位合理推断，"
        f"model_profile（模特画像）请根据商品特性和目标客群设计合适的模特气质、年龄、性别。\n"
        f"输出格式严格按 System Prompt 中的 JSON schema，根节点为 {{\"schemes\": [...]}}，"
        f"schemes 数组长度 = {scheme_count}。"
    )

    print(f"[planning_scheme.stream] 调用: scheme_count={scheme_count}, "
          f"reasoning_effort={effort}, "
          f"product_images={n_prod}",
          flush=True)

    async for delta in pool.stream_chat_with_images(
        system=system_prompt,
        user="\n\n".join(user_text_parts),
        image_uris=product_images,
        response_format={"type": "json_object"},
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
) -> dict[str, Any]:
    """非流式生成 N 套风格迥异的商拍方案。

    ⚠️ Node2 **不使用模特图**：模特图只在 Node3 prompt 生成阶段才喂给 VLM。

    Returns:
        {"schemes": [scheme1_dict, scheme2_dict, ...], "raw_text": str, "thinking_text": str}
    """
    from wellflow.app.config import settings

    pool = get_model_pool()
    effort = reasoning_effort if reasoning_effort is not None else settings.llm_reasoning_effort
    system_prompt = PLANNING_AGENT_SYSTEM_PROMPT

    product_images = product_images or []
    n_prod = len(product_images)

    user_text_parts = [f"【商品识别报告】\n{product_insight}"]
    if user_requirement:
        user_text_parts.append(f"【用户创作需求】\n{user_requirement}")

    if n_prod:
        user_text_parts.append(
            "【参考图编号说明】\n"
            f"共 {n_prod} 张参考图（商品图：服装外观、颜色、细节、材质），编号为 1 ~ {n_prod}。"
        )

    user_text_parts.append(
        f"请结合以上商品识别报告和用户创作需求，输出 **{scheme_count} 套风格迥异、场景互补** 的商拍方案。\n"
        f"三套方案在方案定位、视觉主题、场景设定、模特气质、光影风格上必须有显著差异，避免雷同。\n"
        f"⚠️ 方案中的 target_audience（目标客群）请根据商品定位合理推断，"
        f"model_profile（模特画像）请根据商品特性和目标客群设计合适的模特气质、年龄、性别。\n"
        f"输出格式严格按 System Prompt 中的 JSON schema，根节点为 {{\"schemes\": [...]}}，"
        f"schemes 数组长度 = {scheme_count}。"
    )

    print(f"[planning_scheme] 调用: scheme_count={scheme_count}, "
          f"reasoning_effort={effort}, "
          f"product_images={n_prod}",
          flush=True)

    resp, _used_model = await pool.chat_with_images(
        system=system_prompt,
        user="\n\n".join(user_text_parts),
        image_uris=product_images,
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
