"""Node 3：PromptGeneration — 为每套选中方案生成最终生图 prompt。

输入：
  - scheme: 包含 report_text 的商拍报告载体（来自 Node2 PlanningScheme）
  - product_insight: Node1 的 Markdown 识别报告（可选，补充上下文）
  - product_images: 商品图 data URI 列表
  - reference_images: 三类参考图 {"mannequin": [...], "scene": [...], "outfit": [...]}（可选，data URI）
  - user_requirement: 用户原始创作需求（可选）

输出：
  {"prompt": str, "negative_prompt": str | None, "prompt_detail": dict}

其中 prompt 是 _json_to_natural_prompt() 把 14 维 JSON schema
一字不差拼接成的自然语言（覆盖全部 56 个字段），
negative_prompt 是 negative_prompt 子对象 8 类拼接结果。
prompt_detail 是 VLM 输出的完整 JSON（前端展示/编辑用）。

每套选中方案独立调一次 VLM，由调用方（node3_graph）负责循环 N 次。
"""

from __future__ import annotations

import json
import re
from typing import Any

from wellflow.app.llm.model_pool import get_model_pool
from wellflow.app.prompt.constant import GENERATE_PROMPT_FOR_IMAGE


# ---------------------------------------------------------------------------
# JSON 提取（复用 planning_scheme 的同样逻辑）
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict[str, Any]:
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

    print(f"[prompt_generation] ⚠️ 无法解析 JSON, 原文本前200字: {text[:200]!r}", flush=True)
    return {}


def _extract_prompt_subtitle(raw_text: str, max_chars: int = 6) -> str:
    """从 LLM 原始输出里抽 `#NAME: xxx` 那一行，清理后返回 ≤max_chars 字的 subtitle。

    LLM 按 prompt 约定会在 JSON 之后另起一行输出 `#NAME: 天台逆光剪影` 之类。
    万一没按格式来，就返回空字符串，调用方负责 fallback。
    """
    if not raw_text:
        return ""
    m = re.search(r"(?:^|\n)\s*#NAME\s*[:：]\s*(.+?)\s*$", raw_text, flags=re.MULTILINE)
    if not m:
        return ""
    subtitle = m.group(1).strip()
    # 去掉可能的 markdown / 引号 / 代码块包裹
    subtitle = subtitle.strip("`\"'，。,.；;!！?？、· \t")
    if not subtitle:
        return ""
    # 截断到 max_chars（按字符数，中文=1）
    if len(subtitle) > max_chars:
        subtitle = subtitle[:max_chars]
    return subtitle


# ---------------------------------------------------------------------------
# 把 GENERATE_PROMPT_FOR_IMAGE 的 14 维 JSON schema 一字不差拼成自然语言 prompt
#
# 严格按 schema 字段顺序拼接，覆盖全部 56 个字段路径：
#
#   project        (3 字段)
#   model          (7 字段)
#   clothing       (8 字段)
#   pose           (5 字段)
#   emotion        (1 字段)
#   scene          (6 字段)
#   lighting       (6 字段)
#   camera         (7 字段)
#   post_processing(5 字段)
#   negative_prompt(8 字段, 单独作为 negative_prompt 返回值)
#
#   正向合计：3+7+8+5+1+6+6+7+5 = 48 个字段
#   负面合计：8 个字段
#   总计：56 个字段
# ---------------------------------------------------------------------------


def _collect_non_empty(d: dict[str, Any], keys: list[str]) -> list[str]:
    """按给定顺序从 dict 里取非空且非'无'的字段值，返回字符串列表。"""
    out: list[str] = []
    for k in keys:
        v = d.get(k, "")
        if v and v != "无" and v != "不适用":
            out.append(str(v))
    return out


def _json_to_natural_prompt(detail: dict[str, Any]) -> tuple[str, str | None]:
    """一字不差覆盖 JSON schema 所有字段，拼成正向 prompt + negative_prompt。"""
    if not detail:
        return "", None

    parts: list[str] = []

    # ===== project =====  type, brand_tone, usage
    proj = detail.get("project", {})
    proj_seg = _collect_non_empty(proj, ["type", "brand_tone", "usage"])
    if proj_seg:
        parts.append("摄影类型：" + "、".join(proj_seg))

    # ===== model =====  gender, age, ethnicity, overall_vibe, face_features, skin, hair
    m = detail.get("model", {})
    model_seg = _collect_non_empty(m, ["gender", "age", "ethnicity", "overall_vibe"])
    face_seg = _collect_non_empty(m, ["face_features"])
    skin_seg = _collect_non_empty(m, ["skin"])
    hair_seg = _collect_non_empty(m, ["hair"])
    if model_seg:
        parts.append("模特：" + "、".join(model_seg))
    if face_seg:
        parts.append("面部特征：" + "、".join(face_seg))
    if skin_seg:
        parts.append("皮肤：" + "、".join(skin_seg))
    if hair_seg:
        parts.append("发型：" + "、".join(hair_seg))

    # ===== clothing =====  brand, category, color, color_blocking, silhouette, key_design, structure_details, craft_details
    c = detail.get("clothing", {})
    cl_seg = _collect_non_empty(c, ["brand", "category", "color", "color_blocking",
                                     "silhouette", "key_design"])
    struct_seg = _collect_non_empty(c, ["structure_details"])
    craft_seg = _collect_non_empty(c, ["craft_details"])
    if cl_seg:
        parts.append("服装：" + "、".join(cl_seg))
    if struct_seg:
        parts.append("服装结构细节：" + "、".join(struct_seg))
    if craft_seg:
        parts.append("工艺细节：" + "、".join(craft_seg))

    # ===== pose =====  body_angle, head_direction, hand_action, weight_balance, posture_type
    p = detail.get("pose", {})
    pose_seg = _collect_non_empty(p, ["body_angle", "head_direction", "hand_action",
                                      "weight_balance", "posture_type"])
    if pose_seg:
        parts.append("姿势：" + "、".join(pose_seg))

    # ===== emotion =====  (根节点字符串)
    emo = detail.get("emotion", "")
    if emo and emo != "无" and emo != "不适用":
        parts.append("情绪氛围：" + str(emo))

    # ===== scene =====  location_type, foreground, midground, background, weather_time, material
    s = detail.get("scene", {})
    scene_seg = _collect_non_empty(s, ["location_type", "weather_time", "material",
                                       "background"])
    fg_seg = _collect_non_empty(s, ["foreground"])
    mg_seg = _collect_non_empty(s, ["midground"])
    if scene_seg:
        parts.append("场景：" + "、".join(scene_seg))
    if fg_seg:
        parts.append("前景：" + "、".join(fg_seg))
    if mg_seg:
        parts.append("中景：" + "、".join(mg_seg))

    # ===== lighting =====  time, direction, hard_soft, color_temp, contrast, special_light
    l = detail.get("lighting", {})
    light_seg = _collect_non_empty(l, ["time", "direction", "hard_soft",
                                       "color_temp", "contrast", "special_light"])
    if light_seg:
        parts.append("光线：" + "、".join(light_seg))

    # ===== camera =====  camera_body, lens, focal_length, aperture, iso, angle, depth_of_field
    cam = detail.get("camera", {})
    cam_seg = _collect_non_empty(cam, ["camera_body", "lens", "focal_length",
                                       "aperture", "iso", "angle", "depth_of_field"])
    if cam_seg:
        parts.append("镜头参数：" + "、".join(cam_seg))

    # ===== post_processing =====  sharpness, film_grain, color_tone, contrast, reference_style
    pp = detail.get("post_processing", {})
    pp_seg = _collect_non_empty(pp, ["sharpness", "film_grain", "color_tone",
                                      "contrast", "reference_style"])
    if pp_seg:
        parts.append("后期质感：" + "、".join(pp_seg))

    prompt = "。".join(parts) if parts else ""

    # ===== negative_prompt =====  person, skin, hair, clothing, body_anatomy, visual_style, scene, output
    np_raw = detail.get("negative_prompt", {})
    if np_raw and isinstance(np_raw, dict):
        np_parts: list[str] = []
        for key, label in [
            ("person", "人物"),
            ("skin", "皮肤"),
            ("hair", "发型"),
            ("clothing", "服装"),
            ("body_anatomy", "人体"),
            ("visual_style", "视觉风格"),
            ("scene", "场景"),
            ("output", "输出"),
        ]:
            v = np_raw.get(key, "")
            if v and v != "无" and v != "不适用":
                np_parts.append(f"{label}：{v}")
        negative_prompt = "；".join(np_parts) if np_parts else None
    else:
        negative_prompt = None

    return prompt, negative_prompt


# ---------------------------------------------------------------------------
# 流式
# ---------------------------------------------------------------------------


async def stream_generate_prompt(
    *,
    scheme: dict[str, Any],
    product_insight: str = "",
    product_images: list[str] | None = None,
    reference_images: dict[str, list[str]] | None = None,
    user_requirement: str = "",
    reasoning_effort: str | None = None,
    variant_index: int | None = None,
    variant_total: int | None = None,
):
    """流式为单个方案的一个变体生成最终 prompt（yield {"type": "thinking"|"content", "text": "..."}）。

    variant_index / variant_total 为 None 时视为"单变体"，不追加变体提示。
    """
    from wellflow.app.config import settings

    pool = get_model_pool()
    effort = reasoning_effort if reasoning_effort is not None else settings.node3_reasoning_effort

    system_prompt = GENERATE_PROMPT_FOR_IMAGE

    # user message：把商拍报告正文 + 识别报告 + 用户需求喂给 VLM
    scheme_index = scheme.get("scheme_index", "?")
    scheme_name = scheme.get("scheme_name", f"方案{scheme_index}")
    scheme_report = scheme.get("report_text")
    user_text_parts = [f"【目标方案】方案 #{scheme_index} · {scheme_name}"]
    if not isinstance(scheme_report, str) or not scheme_report.strip():
        raise ValueError("Node2 商拍策划报告正文为空")
    user_text_parts.append(f"【完整商拍策划报告】\n{scheme_report}")
    if product_insight:
        user_text_parts.append(f"【商品识别报告】\n{product_insight}")
    if user_requirement:
        user_text_parts.append(f"【用户原始需求】\n{user_requirement}")

    # 变体提示：同一方案多份 prompt 时，明确要求不同的构图/镜头/氛围
    if variant_index is not None and variant_total and variant_total > 1:
        user_text_parts.append(
            f"【变体要求】当前为该方案第 {variant_index + 1} / {variant_total} 份 prompt，"
            "请在保持方案核心卖点与商品一致性的前提下，主动变化镜头语言、构图、"
            "光影、模特姿势或场景氛围，使这一份与生图结果和同方案的其他变体明显区分。"
        )

    # —— refs 结构化拆分 ——
    product_images = product_images or []
    ref_images = reference_images or {}
    mannequin_images = ref_images.get("mannequin") or []
    scene_images = ref_images.get("scene") or []
    outfit_images = ref_images.get("outfit") or []

    # 固定顺序拼接：商品图 → mannequin → scene → outfit
    all_images = product_images + mannequin_images + scene_images + outfit_images

    # —— 编号范围计算 ——
    n_prod = len(product_images)
    n_m = len(mannequin_images)
    n_s = len(scene_images)
    n_o = len(outfit_images)

    start_m = n_prod + 1 if n_prod else 1
    end_m = start_m + n_m - 1 if n_m else start_m - 1
    start_s = end_m + 1 if n_m else start_m
    end_s = start_s + n_s - 1 if n_s else start_s - 1
    start_o = end_s + 1 if n_s else start_s
    end_o = start_o + n_o - 1 if n_o else start_o - 1

    has_any_ref = n_prod + n_m + n_s + n_o > 0
    if has_any_ref:
        ref_lines = [f"共 {n_prod + n_m + n_s + n_o} 张参考图，编号含义如下："]
        if n_prod:
            ref_lines.append(f"• 第 1 ~ {n_prod} 张（共 {n_prod} 张）= 商品图（服装外观、颜色、细节、材质，用于保证商品一致性）")
        if n_m:
            ref_lines.append(f"• 第 {start_m} ~ {end_m} 张（共 {n_m} 张）= 模特参考图（人脸、体型、气质，用于保证模特特征一致性）")
        if n_s:
            ref_lines.append(f"• 第 {start_s} ~ {end_s} 张（共 {n_s} 张）= 场景参考图（环境、光线、氛围，用于保证场景一致性）")
        if n_o:
            ref_lines.append(f"• 第 {start_o} ~ {end_o} 张（共 {n_o} 张）= 穿搭参考图（服装搭配、叠穿、配饰，用于保证穿搭呈现一致性）")
        user_text_parts.append("【参考图编号说明】\n" + "\n".join(ref_lines))

    # —— 按类型注入约束（每类只在有内容时注入）——
    if n_m:
        user_text_parts.append(
            f"【模特参考图约束】\n"
            f"请严格参考第 {start_m} ~ {end_m} 张模特参考图的人脸/体型/气质特征，"
            f"确保输出的 14 维 JSON 中 model 字段（性别、年龄、脸型、五官、肤色、发型、体型、气质）"
            f"与参考图高度一致，不得自行替换或臆造模特特征。"
        )
    if n_s:
        user_text_parts.append(
            f"【场景参考图约束】\n"
            f"请严格参考第 {start_s} ~ {end_s} 张场景参考图的环境、光线、氛围、空间布局，"
            f"确保输出的 14 维 JSON 中 scene 字段（location_type、background、material、weather_time）"
            f"与参考图高度一致，不得自行臆造未出现的场景元素。"
        )
    if n_o:
        user_text_parts.append(
            f"【穿搭参考图约束】\n"
            f"请严格参考第 {start_o} ~ {end_o} 张穿搭参考图的服装搭配、叠穿方式、配饰、整体造型逻辑，"
            f"确保输出的 14 维 JSON 中 clothing 字段（category、color_blocking、silhouette、key_design）"
            f"与参考图一致，不得自行修改服装的搭配组合。"
        )

    user_text_parts.append(
        f"请按 System Prompt 的 14 维结构输出 JSON。"
    )

    async for delta in pool.stream_chat_with_images(
        system=system_prompt,
        user="\n\n".join(user_text_parts),
        image_uris=all_images,
        reasoning_effort=effort,
    ):
        if delta:
            yield delta


# ---------------------------------------------------------------------------
# 非流式
# ---------------------------------------------------------------------------


async def generate_prompt_for_scheme(
    *,
    scheme: dict[str, Any],
    product_insight: str = "",
    product_images: list[str] | None = None,
    reference_images: dict[str, list[str]] | None = None,
    user_requirement: str = "",
    reasoning_effort: str | None = None,
    variant_index: int | None = None,
    variant_total: int | None = None,
) -> dict[str, Any]:
    """为单个方案的一个变体生成最终 prompt。

    variant_index / variant_total 为 None 时视为"单变体"，不追加变体提示。

    Returns:
        {"prompt": str, "negative_prompt": str | None, "prompt_detail": dict,
         "raw_text": str, "thinking_text": str, "scheme_index": int | None}
    """
    from wellflow.app.config import settings

    pool = get_model_pool()
    effort = reasoning_effort if reasoning_effort is not None else settings.node3_reasoning_effort

    system_prompt = GENERATE_PROMPT_FOR_IMAGE

    scheme_index = scheme.get("scheme_index")
    scheme_name = scheme.get("scheme_name", f"方案{scheme_index}")
    scheme_report = scheme.get("report_text")
    user_text_parts = [f"【目标方案】方案 #{scheme_index} · {scheme_name}"]
    if not isinstance(scheme_report, str) or not scheme_report.strip():
        raise ValueError("Node2 商拍策划报告正文为空")
    user_text_parts.append(f"【完整商拍策划报告】\n{scheme_report}")
    if product_insight:
        user_text_parts.append(f"【商品识别报告】\n{product_insight}")
    if user_requirement:
        user_text_parts.append(f"【用户原始需求】\n{user_requirement}")

    # 变体提示
    if variant_index is not None and variant_total and variant_total > 1:
        user_text_parts.append(
            f"【变体要求】当前为该方案第 {variant_index + 1} / {variant_total} 份 prompt，"
            "请在保持方案核心卖点与商品一致性的前提下，主动变化镜头语言、构图、"
            "光影、模特姿势或场景氛围，使这一份与生图结果和同方案的其他变体明显区分。"
        )

    # —— refs 结构化拆分 ——
    product_images = product_images or []
    ref_images = reference_images or {}
    mannequin_images = ref_images.get("mannequin") or []
    scene_images = ref_images.get("scene") or []
    outfit_images = ref_images.get("outfit") or []

    all_images = product_images + mannequin_images + scene_images + outfit_images

    n_prod = len(product_images)
    n_m = len(mannequin_images)
    n_s = len(scene_images)
    n_o = len(outfit_images)

    start_m = n_prod + 1 if n_prod else 1
    end_m = start_m + n_m - 1 if n_m else start_m - 1
    start_s = end_m + 1 if n_m else start_m
    end_s = start_s + n_s - 1 if n_s else start_s - 1
    start_o = end_s + 1 if n_s else start_s
    end_o = start_o + n_o - 1 if n_o else start_o - 1

    has_any_ref = n_prod + n_m + n_s + n_o > 0
    if has_any_ref:
        ref_lines = [f"共 {n_prod + n_m + n_s + n_o} 张参考图，编号含义如下："]
        if n_prod:
            ref_lines.append(f"• 第 1 ~ {n_prod} 张（共 {n_prod} 张）= 商品图（服装外观、颜色、细节、材质，用于保证商品一致性）")
        if n_m:
            ref_lines.append(f"• 第 {start_m} ~ {end_m} 张（共 {n_m} 张）= 模特参考图（人脸、体型、气质，用于保证模特特征一致性）")
        if n_s:
            ref_lines.append(f"• 第 {start_s} ~ {end_s} 张（共 {n_s} 张）= 场景参考图（环境、光线、氛围，用于保证场景一致性）")
        if n_o:
            ref_lines.append(f"• 第 {start_o} ~ {end_o} 张（共 {n_o} 张）= 穿搭参考图（服装搭配、叠穿、配饰，用于保证穿搭呈现一致性）")
        user_text_parts.append("【参考图编号说明】\n" + "\n".join(ref_lines))

    if n_m:
        user_text_parts.append(
            f"【模特参考图约束】\n"
            f"请严格参考第 {start_m} ~ {end_m} 张模特参考图的人脸/体型/气质特征，"
            f"确保输出的 14 维 JSON 中 model 字段（性别、年龄、脸型、五官、肤色、发型、体型、气质）"
            f"与参考图高度一致，不得自行替换或臆造模特特征。"
        )
    if n_s:
        user_text_parts.append(
            f"【场景参考图约束】\n"
            f"请严格参考第 {start_s} ~ {end_s} 张场景参考图的环境、光线、氛围、空间布局，"
            f"确保输出的 14 维 JSON 中 scene 字段（location_type、background、material、weather_time）"
            f"与参考图高度一致，不得自行臆造未出现的场景元素。"
        )
    if n_o:
        user_text_parts.append(
            f"【穿搭参考图约束】\n"
            f"请严格参考第 {start_o} ~ {end_o} 张穿搭参考图的服装搭配、叠穿方式、配饰、整体造型逻辑，"
            f"确保输出的 14 维 JSON 中 clothing 字段（category、color_blocking、silhouette、key_design）"
            f"与参考图一致，不得自行修改服装的搭配组合。"
        )

    user_text_parts.append("请按 System Prompt 的 14 维结构输出 JSON。")

    _variant_label = (
        f" variant {variant_index+1}/{variant_total}"
        if variant_index is not None and variant_total
        else ""
    )
    print(f"[prompt_generation] 调用: scheme #{scheme_index}{_variant_label}, "
          f"product_images={n_prod}, mannequin={n_m}, scene={n_s}, outfit={n_o}, "
          f"reasoning_effort={effort}",
          flush=True)

    resp, used_model = await pool.chat_with_images(
        system=system_prompt,
        user="\n\n".join(user_text_parts),
        image_uris=all_images,
        response_format={"type": "json_object"},
        reasoning_effort=effort,
    )

    raw = resp.content or ""
    thinking_text = getattr(resp, "thinking", None)
    print(f"[prompt_generation] scheme #{scheme_index}{_variant_label}: 原始文本 len={len(raw)}, "
          f"thinking={len(thinking_text) if thinking_text else 0}", flush=True)

    detail = _extract_json(raw)
    prompt, negative_prompt = _json_to_natural_prompt(detail)
    subtitle = _extract_prompt_subtitle(raw)

    print(f"[prompt_generation] ✅ scheme #{scheme_index}{_variant_label}: prompt len={len(prompt)}, "
          f"negative_prompt={'有' if negative_prompt else '无'}, "
          f"subtitle={subtitle!r}", flush=True)

    return {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "prompt_detail": detail,
        "raw_text": raw,
        "thinking_text": thinking_text,
        "scheme_index": scheme_index,
        "scheme_name": scheme_name,
        "prompt_subtitle": subtitle,
    }
