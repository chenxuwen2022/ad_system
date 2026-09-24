"""Node3：按选定商拍方案一次生成多份中文生图提示词。"""

from __future__ import annotations

import json
from typing import Any

from wellflow.app.llm.model_pool import get_model_pool
from wellflow.app.prompt.registry import get_active_prompt


def _reference_groups(
    product_images: list[str] | None,
    reference_images: dict[str, list[str]] | None,
) -> list[tuple[list[str], str, str]]:
    """唯一的参考图顺序与职责定义，供图片发送和绑定生成共用。"""
    refs = reference_images or {}
    return [
        (product_images or [], "锁定商品",
         "严格还原服装、Logo、结构、颜色和材质；不参考人脸、姿态和背景"),
        (refs.get("mannequin") or [], "锁定模特",
         "识别并严格保持指定模特图中同一人物的五官、脸型、发型、肤色、体型和气质，"
         "不得换人；不参考服装款式与场景"),
        (refs.get("scene") or [], "锁定场景",
         "严格使用指定场景图的环境、空间层次和光影，优先于策划方案中的场景；"
         "不参考人脸与服装款式"),
        (refs.get("outfit") or [], "参考穿搭",
         "识别并遵循指定穿搭图的搭配、叠穿与配饰，不得覆盖商品图中的主产品；"
         "不参考人脸与场景"),
    ]


def reference_binding_block(
    product_images: list[str] | None,
    reference_images: dict[str, list[str]] | None,
) -> str:
    """按实际发送顺序生成绑定块，提示词与输出校验共用此结果。"""
    lines: list[str] = []
    index = 1
    for group, action, detail in _reference_groups(product_images, reference_images):
        if not group:
            continue
        end = index + len(group) - 1
        number = str(index) if end == index else f"{index}–{end}"
        lines.append(f"第 {number} 张{action}：{detail}。")
        index = end + 1
    return "【参考图绑定】\n" + "\n".join(lines) if lines else ""


def _reference_instructions(binding: str) -> str:
    """绑定块只注入一次，通用规则不再重复各类图片的具体职责。"""
    if not binding:
        return ""
    return (
        "【参考图强制规则（优先于前述数据库模板与策划报告）】\n"
        "逐张按以下编号和职责识别参考图，并在正文描述识别到的对应特征。"
        "参考图在各自职责范围内优先于数据库模板、识别报告和策划方案中的描述、"
        "示例及默认设定；不同职责不得串图，未提供的类型不得虚构图号。\n"
        "每份最终 prompt 的正文必须以以下完整绑定块开头，逐字保留所有行，"
        "然后空一行再写完整生图描述。每份都必须独立携带，不得只在第一份写、"
        "不得用外貌描述代替绑定、不得放在提示词正文之外，也不得在后文否定这些约束：\n"
        + binding
    )


def split_generated_prompts(
    raw: str, expected_count: int, *, required_binding: str = "",
) -> list[dict[str, str]]:
    """校验完整 JSON 提示词列表、数量及每份参考图绑定。"""
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("生图提示词格式解析失败：需要完整的 JSON 数组") from exc
    if not isinstance(items, list) or not items:
        raise ValueError("生图提示词格式解析失败：需要非空提示词数组")
    if len(items) != expected_count:
        raise ValueError(f"生图提示词数量不符：期望 {expected_count} 份，实际 {len(items)} 份")
    prompts: list[dict[str, str]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index + 1} 份生图提示词必须是对象")
        for field in ("title", "prompt"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"第 {index + 1} 份生图提示词缺少有效的 {field}")
        body = item["prompt"].strip()
        if required_binding:
            # 固定前缀必须完整保留，不能仅靠正文里出现“模特”等关键词通过校验。
            binding, separator, description = body.partition("\n\n")
            if binding != required_binding or not separator or not description.strip():
                raise ValueError(
                    f"第 {index + 1} 份生图提示词缺少完整的参考图识别绑定或正文，请重新生成"
                )
        prompts.append({"title": item["title"].strip(), "prompt": body})
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

    images = [
        image
        for group, _, _ in _reference_groups(product_images, reference_images)
        for image in group
    ]

    lines = [
        f"【目标商拍方案】{scheme.get('scheme_name', '商拍方案')}",
        f"【策划报告】\n{report}",
    ]
    if product_insight:
        lines.append(f"【识别报告】\n{product_insight}")
    if user_requirement:
        lines.append(f"【用户当前要求】\n{user_requirement}")

    binding = reference_binding_block(product_images, reference_images)
    system_parts = [get_active_prompt("image_prompt")]
    if binding:
        system_parts.append(_reference_instructions(binding))
    system_parts.append(
        f"【本次输出要求】针对上述同一套商拍方案，一次生成 {prompt_count} 份风格与镜头表达不同的完整中文生图提示词。"
        "各份均须完整覆盖前述数据库提示词定义的全部维度，维度数量与内容以该提示词为准。"
    )
    system_parts.append(
        "【本次输出协议｜覆盖前文中冲突的数量、格式要求】只输出一个合法 JSON 数组，"
        f"数组必须恰好包含 {prompt_count} 个提示词对象，不能增减。"
        "每个对象按顺序输出 title、prompt 两个字符串字段；title 为简短标题，"
        "prompt 为包含完整参考图绑定块及生图描述的中文正文。"
        "使用标准 JSON 转义换行、双引号和反斜杠；不要代码块、前言、结语或分段标记。"
        "数组结构示例（请替换为真实提示词）："
        + json.dumps([
            {"title": f"提示词{i + 1}标题", "prompt": "完整参考图绑定块及生图描述"}
            for i in range(prompt_count)
        ], ensure_ascii=False)
    )
    return "\n\n".join(system_parts), "\n\n".join(lines), images


async def stream_generate_prompt(
    *,
    scheme: dict[str, Any],
    product_insight: str = "",
    product_images: list[str] | None = None,
    reference_images: dict[str, list[str]] | None = None,
    user_requirement: str = "",
    prompt_count: int,
    reasoning_effort: str | None = None,
    task_id: str | None = None,
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
    pool = get_model_pool(task_id=task_id)
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
    task_id: str | None = None,
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
    pool = get_model_pool(task_id=task_id)
    effort = reasoning_effort if reasoning_effort is not None else settings.node3_reasoning_effort
    response, model = await pool.chat_with_images(
        system=system, user=user, image_uris=images, reasoning_effort=effort,
    )
    raw = response.content or ""
    return {
        "prompts": split_generated_prompts(
            raw, prompt_count,
            required_binding=reference_binding_block(product_images, reference_images),
        ),
        "raw_text": raw,
        "thinking_text": getattr(response, "thinking", None),
        "model": model,
    }
