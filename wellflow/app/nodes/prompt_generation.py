"""Node3：按选定商拍方案一次生成多份中文生图提示词。"""

from __future__ import annotations

import re
from typing import Any

from wellflow.app.llm.model_pool import get_model_pool
from wellflow.app.prompt.registry import get_active_prompt

_PROMPT_HEADER = re.compile(r"^===PROMPT ([1-9]\d*): (.+?)===$", re.MULTILINE)


def split_generated_prompts(raw: str, expected_count: int) -> list[dict[str, str]]:
    """拆分一次模型调用返回的多份纯文本提示词。"""
    matches = list(_PROMPT_HEADER.finditer(raw))
    if not matches or raw[:matches[0].start()].strip():
        raise ValueError("生图提示词缺少规范的分段边界")
    prompts: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        if int(match.group(1)) != index + 1:
            raise ValueError("生图提示词编号不连续")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        body = raw[match.end():end].strip()
        if not body:
            raise ValueError(f"第 {index + 1} 份生图提示词为空")
        prompts.append({"title": match.group(2).strip(), "prompt": body})
    if len(prompts) != expected_count:
        raise ValueError(f"生图提示词数量不符：期望 {expected_count} 份，实际 {len(prompts)} 份")
    return prompts


def _build_request(
    *,
    scheme: dict[str, Any],
    product_insight: str,
    product_images: list[str] | None,
    reference_images: dict[str, list[str]] | None,
    user_requirement: str,
    prompt_count: int,
) -> tuple[str, str, list[str]]:
    report = scheme.get("report_text")
    if not isinstance(report, str) or not report.strip():
        raise ValueError("Node2 商拍策划报告正文为空")
    if prompt_count < 1:
        raise ValueError("prompt_count 必须大于 0")

    product_images = product_images or []
    reference_images = reference_images or {}
    mannequin = reference_images.get("mannequin") or []
    scene = reference_images.get("scene") or []
    outfit = reference_images.get("outfit") or []
    images = product_images + mannequin + scene + outfit

    lines = [
        f"【目标商拍方案】{scheme.get('scheme_name', '商拍方案')}",
        f"【策划报告】\n{report}",
    ]
    if product_insight:
        lines.append(f"【识别报告】\n{product_insight}")
    if user_requirement:
        lines.append(f"【用户当前要求】\n{user_requirement}")

    image_lines: list[str] = []
    index = 1
    for label, group, role, ignore in (
        ("服饰产品图", product_images, "服装、Logo、结构、颜色和材质", "人脸、姿态和背景"),
        ("模特图", mannequin, "人脸、发型、体型和人物气质", "服装款式与场景"),
        ("场景图", scene, "环境、空间层次和光影", "人脸与服装款式"),
        ("穿搭图", outfit, "搭配、叠穿与配饰", "主产品结构、人脸与场景"),
    ):
        for _ in group:
            image_lines.append(f"第 {index} 张：{label}；优先参考 {role}；不参考 {ignore}。")
            index += 1
    system = (
        get_active_prompt("image_prompt")
        + ("\n\n【本次参考图编号与职责】\n" + "\n".join(image_lines) if image_lines else "")
        + f"\n\n【本次输出要求】针对上述同一套商拍方案，一次生成 {prompt_count} 份风格与镜头表达不同的完整中文生图提示词。"
          "各份均须遵守上面18条要求，并严格锁定同一产品与指定参考图。不要输出 JSON。"
          "每份开头单独一行使用精确边界：===PROMPT 1: 简短标题===、"
          "===PROMPT 2: 简短标题===，依此类推；提示词正文从下一行开始。"
          "不要在正文其他位置使用这个边界格式，也不要输出边界以外的说明。"
    )
    return system, "\n\n".join(lines), images


async def stream_generate_prompt(
    *,
    scheme: dict[str, Any],
    product_insight: str = "",
    product_images: list[str] | None = None,
    reference_images: dict[str, list[str]] | None = None,
    user_requirement: str = "",
    prompt_count: int,
    reasoning_effort: str | None = None,
):
    """一次流式模型调用，返回该方案的全部提示词文本。"""
    from wellflow.app.config import settings

    system, user, images = _build_request(
        scheme=scheme,
        product_insight=product_insight,
        product_images=product_images,
        reference_images=reference_images,
        user_requirement=user_requirement,
        prompt_count=prompt_count,
    )
    pool = get_model_pool()
    effort = reasoning_effort if reasoning_effort is not None else settings.node3_reasoning_effort
    async for delta in pool.stream_chat_with_images(
        system=system, user=user, image_uris=images, reasoning_effort=effort,
    ):
        if delta:
            yield delta


async def generate_prompt_for_scheme(
    *,
    scheme: dict[str, Any],
    product_insight: str = "",
    product_images: list[str] | None = None,
    reference_images: dict[str, list[str]] | None = None,
    user_requirement: str = "",
    prompt_count: int,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """非流式入口，同样一次调用返回全部提示词。"""
    from wellflow.app.config import settings

    system, user, images = _build_request(
        scheme=scheme,
        product_insight=product_insight,
        product_images=product_images,
        reference_images=reference_images,
        user_requirement=user_requirement,
        prompt_count=prompt_count,
    )
    pool = get_model_pool()
    effort = reasoning_effort if reasoning_effort is not None else settings.node3_reasoning_effort
    response, model = await pool.chat_with_images(
        system=system, user=user, image_uris=images, reasoning_effort=effort,
    )
    raw = response.content or ""
    return {
        "prompts": split_generated_prompts(raw, prompt_count),
        "raw_text": raw,
        "thinking_text": getattr(response, "thinking", None),
        "model": model,
    }
