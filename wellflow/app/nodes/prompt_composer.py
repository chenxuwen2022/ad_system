"""Node 3：PromptComposer（final.md 第 7.1 节）。

把 ProductProfile + brand + plan + shot + skill + platform + 反馈 组装为最终 prompt。
可选 LLM 润色，但不能改变商品事实或删除保真约束。
"""

from __future__ import annotations

from typing import Any


FIDELITY_RULES = [
    "不改变款式",
    "不改变颜色",
    "不改变结构",
    "不改变 Logo / 图案",
    "不改变关键细节",
    "不虚构功能或工艺",
    "保留已确认的材质视觉特征",
]


def compose_prompt(
    *,
    product_profile: dict[str, Any],
    brand_config: dict[str, Any],
    plan: dict[str, Any],
    shot: dict[str, Any],
    skill: str,
    platform: str,
    feedback: str = "",
) -> str:
    parts: list[str] = []

    # 1. 保真规则（必须写入）
    parts.append("【保真约束】")
    parts.extend(f"- {rule}" for rule in FIDELITY_RULES)

    # 2. 商品事实
    parts.append("\n【商品信息】")
    parts.append(str(product_profile))

    # 3. 品牌规范
    if brand_config:
        parts.append("\n【品牌规范】")
        parts.append(str(brand_config))

    # 4. 方案与镜头
    parts.append(f"\n【商拍方案：{plan.get('name', '')}】")
    parts.append(f"方向：{plan.get('direction', '')}")
    parts.append(f"概念：{plan.get('concept', '')}")
    parts.append(f"调性：{plan.get('tone', '')}")
    parts.append(f"镜头（{shot.get('shot_id', '')} / {shot.get('shot_type', '')}）")
    parts.append(f"目标：{shot.get('objective', '')}")
    if shot.get("lighting_notes"):
        parts.append(f"光影：{shot['lighting_notes']}")
    if shot.get("composition_notes"):
        parts.append(f"构图：{shot['composition_notes']}")
    if shot.get("garment_focus"):
        parts.append(f"重点部位：{', '.join(shot['garment_focus'])}")

    # 5. skill 与平台
    parts.append(f"\n【执行 skill】{skill}")
    parts.append(f"【目标平台】{platform}")

    # 6. 用户/QA 反馈
    if feedback:
        parts.append(f"\n【反馈修正】{feedback}")

    return "\n".join(parts)
