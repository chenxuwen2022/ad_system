"""Node 1：InputAnalyzer（final.md 第 5.2 节）。

职责：校验输入、归档输入资产、判断是否需要视觉预处理。
"""

from __future__ import annotations

from typing import Any

from wellflow.app.errors import business_error, INPUT_REQUIRED


def analyze_input(
    *,
    has_images: bool,
    has_text: bool,
    image_count: int = 0,
    image_qualities: list[float] | None = None,
) -> dict[str, Any]:
    """纯规则分析，不调用 LLM。

    返回 InputAnalysis dict；若输入完全为空则抛业务错误。
    """
    if not has_images and not has_text:
        raise business_error(*INPUT_REQUIRED, source="input_analyzer")

    quality_scores = image_qualities or []
    avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 1.0

    # --- 启发式预处理判断（final.md 第 5.3 节）---
    preprocessing_reasons: list[str] = []
    if avg_quality < 0.7 and has_images:
        preprocessing_reasons.append("图片质量偏低")
    if image_count >= 1 and avg_quality < 0.85:
        preprocessing_reasons.append("可能需要增强面料纹理")
    needs_preprocessing = bool(preprocessing_reasons)

    return {
        "has_images": has_images,
        "has_text": has_text,
        "image_count": image_count,
        "quality_score": round(avg_quality, 3),
        "needs_preprocessing": needs_preprocessing,
        "preprocessing_reasons": preprocessing_reasons,
    }
