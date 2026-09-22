"""Node 2：PlanningScheme — VLM 多模态产出多套完整商拍方案正文。

输入：Node 1 的 product_insight（Markdown 报告）+ 产品图（data URI 列表）
      + 用户原始需求
输出：带纯文本方案边界的多套报告；后端拆分为 C2 可选择的方案列表。

⚠️ Node2 **不使用模特图**：模特图只在 Node3 prompt 生成阶段才喂给 VLM，
   让 Node3 在最终生图提示词里严格对齐模特的人脸/体型/气质。
   Node2 只负责基于商品和用户需求产出最终商拍方案。

使用数据库中已发布的商拍策划提示词，不约束模型返回 JSON。
"""

from __future__ import annotations

import re
from typing import Any

from wellflow.app.llm.model_pool import get_model_pool
from wellflow.app.prompt.registry import get_active_prompt


SCHEME_HEADER = re.compile(r"^===SCHEME ([1-9]\d*): (.+?)===$", re.MULTILINE)


def split_scheme_reports(raw: str, expected_count: int | None = None) -> list[dict[str, Any]]:
    """按明确的纯文本边界拆分报告，不解析报告正文中的章节。"""
    matches = list(SCHEME_HEADER.finditer(raw))
    if not matches or raw[:matches[0].start()].strip():
        raise ValueError("商拍方案缺少规范的方案边界")
    schemes = []
    for index, match in enumerate(matches):
        if int(match.group(1)) != index + 1:
            raise ValueError("商拍方案编号不连续")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        report = raw[match.end():end].strip()
        if not report:
            raise ValueError(f"第 {index + 1} 套商拍方案正文为空")
        schemes.append({"scheme_index": index, "scheme_name": match.group(2).strip(), "report_text": report})
    if expected_count is not None and len(schemes) != expected_count:
        raise ValueError(f"商拍方案数量不符：期望 {expected_count} 套，实际 {len(schemes)} 套")
    return schemes


def planning_system_prompt(scheme_count: int) -> str:
    return (
        get_active_prompt("shoot_plan")
        + f"\n\n【本次首次生成要求】一次生成 {scheme_count} 套互不相同、风格迥异的候选商拍策划方案。"
          "每套均须独立覆盖上述全部要求；方案之间在视觉主题、使用与拍摄场景、"
          "模特气质、穿搭、镜头语言和光影表达上形成明显区别，同时都必须忠于产品报告。"
          "不要输出 JSON。"
          "每套方案开头单独一行使用精确边界：===SCHEME 1: 方案名称===、"
          "===SCHEME 2: 方案名称===，依此类推；方案正文从下一行开始。"
          "不要在正文其他位置使用这个边界格式，也不要输出边界以外的说明。"
    )


async def stream_plan_schemes(
    *,
    product_insight: str,
    product_images: list[str] | None = None,
    user_requirement: str = "",
    scheme_count: int | None = None,
    reasoning_effort: str | None = None,
):
    """流式生成多套商拍方案，yield {"type": "thinking"|"content", "text": "..."}。

    ⚠️ Node2 **不使用模特图**：模特图只在 Node3 prompt 生成阶段才喂给 VLM。
    """
    from wellflow.app.config import settings

    pool = get_model_pool()
    effort = reasoning_effort if reasoning_effort is not None else settings.node2_reasoning_effort
    scheme_count = scheme_count or settings.node2_scheme_count_default
    system_prompt = planning_system_prompt(scheme_count)

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
        "请根据商品识别报告和用户创作需求，按系统提示词要求输出各套完整方案。"
        "无需 JSON、代码块或字段包装。"
    )

    print(f"[planning_scheme.stream] 调用: reasoning_effort={effort}, "
          f"product_images={n_prod}",
          flush=True)

    async for delta in pool.stream_chat_with_images(
        system=system_prompt,
        user="\n\n".join(user_text_parts),
        image_uris=product_images,
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
    scheme_count: int | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """非流式生成多套商拍方案。

    ⚠️ Node2 **不使用模特图**：模特图只在 Node3 prompt 生成阶段才喂给 VLM。

    Returns:
        {"schemes": [{"report_text": str, ...}], "raw_text": str, "thinking_text": str}
    """
    from wellflow.app.config import settings

    pool = get_model_pool()
    effort = reasoning_effort if reasoning_effort is not None else settings.node2_reasoning_effort
    scheme_count = scheme_count or settings.node2_scheme_count_default
    system_prompt = planning_system_prompt(scheme_count)

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
        "请根据商品识别报告和用户创作需求，按系统提示词要求输出各套完整方案。"
        "无需 JSON、代码块或字段包装。"
    )

    print(f"[planning_scheme] 调用: reasoning_effort={effort}, "
          f"product_images={n_prod}",
          flush=True)

    resp, _used_model = await pool.chat_with_images(
        system=system_prompt,
        user="\n\n".join(user_text_parts),
        image_uris=product_images,
        reasoning_effort=effort,
    )

    raw = resp.content or ""
    thinking_text = getattr(resp, "thinking", None)
    print(f"[planning_scheme] VLM 返回原始文本 len={len(raw)}, "
          f"thinking={len(thinking_text) if thinking_text else 0}, 前100字={raw[:100]!r}", flush=True)

    schemes = split_scheme_reports(raw, expected_count=scheme_count)

    return {
        "schemes": schemes,
        "raw_text": raw,
        "thinking_text": thinking_text,
    }
