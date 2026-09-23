"""Node 2：PlanningScheme — VLM 多模态产出多套完整商拍方案正文。

输入：Node 1 的 product_insight（Markdown 报告）+ 产品图（data URI 列表）
      + 用户原始需求
输出：包含配置数量方案的 JSON 数组；后端校验后供 C2 选择。

⚠️ Node2 **不使用模特图**：模特图只在 Node3 prompt 生成阶段才喂给 VLM，
   让 Node3 在最终生图提示词里严格对齐模特的人脸/体型/气质。
   Node2 只负责基于商品和用户需求产出最终商拍方案。

使用数据库中已发布的商拍策划提示词，追加统一的 JSON 数组输出协议。
"""

from __future__ import annotations

import json
from typing import Any

from wellflow.app.llm.model_pool import get_model_pool
from wellflow.app.prompt.registry import get_active_prompt


def split_scheme_reports(raw: str, expected_count: int | None = None) -> list[dict[str, Any]]:
    """校验完整 JSON 数组；数量错误不得截断后伪装成生成成功。"""
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("商拍方案格式解析失败：需要完整的 JSON 数组") from exc
    if not isinstance(items, list) or not items:
        raise ValueError("商拍方案格式解析失败：需要非空方案数组")
    if expected_count is not None and len(items) != expected_count:
        raise ValueError(f"商拍方案数量不符：期望 {expected_count} 套，实际 {len(items)} 套")
    schemes = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index + 1} 套商拍方案必须是对象")
        for field in ("scheme_name", "report_text"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"第 {index + 1} 套商拍方案缺少有效的 {field}")
        schemes.append({"scheme_index": index, "scheme_name": item["scheme_name"].strip(),
                        "report_text": item["report_text"].strip()})
    return schemes


def scheme_output_contract(scheme_count: int) -> str:
    if scheme_count < 1:
        raise ValueError("商拍方案数量必须大于零")
    example = json.dumps([
        {"scheme_name": f"方案{i + 1}名称", "report_text": "该方案完整正文（Markdown）"}
        for i in range(scheme_count)
    ], ensure_ascii=False)
    return (
        f"\n\n【本次输出协议｜覆盖前文中冲突的数量、格式要求】只输出一个合法 JSON 数组，"
        f"数组必须恰好包含 {scheme_count} 个方案对象，不能增减。"
        "每个对象按顺序输出 scheme_name、report_text 两个字符串字段。"
        "report_text 包含该套方案的完整 Markdown 正文，不再嵌入其他候选方案。"
        "使用标准 JSON 转义换行、双引号和反斜杠；不要代码块、前言、结语或 SCHEME 分隔符。"
        f"数组结构示例（请替换为真实方案）：{example}"
    )


def planning_system_prompt(scheme_count: int) -> str:
    return (
        get_active_prompt("shoot_plan")
        + f"\n\n【本次生成要求（首次及重新生成）】一次生成 {scheme_count} 套互不相同、风格迥异的候选商拍策划方案。"
          "每套均须独立覆盖上述全部要求；方案之间在视觉主题、使用与拍摄场景、"
          "模特气质、穿搭、镜头语言和光影表达上形成明显区别，同时都必须忠于产品报告。"
        + scheme_output_contract(scheme_count)
    )


async def stream_plan_schemes(
    *,
    product_insight: str,
    product_images: list[str] | None = None,
    user_requirement: str = "",
    scheme_count: int | None = None,
    reasoning_effort: str | None = None,
    task_id: str | None = None,
):
    """流式生成多套商拍方案，yield {"type": "thinking"|"content", "text": "..."}。

    ⚠️ Node2 **不使用模特图**：模特图只在 Node3 prompt 生成阶段才喂给 VLM。
    """
    from wellflow.app.config import settings

    pool = get_model_pool(task_id=task_id)
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
        "仅输出系统要求的 JSON 方案数组。"
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
    task_id: str | None = None,
) -> dict[str, Any]:
    """非流式生成多套商拍方案。

    ⚠️ Node2 **不使用模特图**：模特图只在 Node3 prompt 生成阶段才喂给 VLM。

    Returns:
        {"schemes": [{"report_text": str, ...}], "raw_text": str, "thinking_text": str}
    """
    from wellflow.app.config import settings

    pool = get_model_pool(task_id=task_id)
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
        "仅输出系统要求的 JSON 方案数组。"
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
