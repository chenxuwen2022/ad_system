"""Node 3：PromptGeneration — 为每套选中方案生成最终生图 prompt。

输入：
  - scheme: 单个 12 维商拍方案 JSON（来自 Node2 PlanningScheme）
  - product_insight: Node1 的 Markdown 识别报告（可选，补充上下文）
  - product_images: 商品图 data URI 列表
  - model_images: 用户上传的模特图 data URI 列表（可选）
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
from wellflow.app.prompt.constant import GENERATE_IMAGE_PROMPT


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


# ---------------------------------------------------------------------------
# 把 GENERATE_IMAGE_PROMPT 的 14 维 JSON schema 一字不差拼成自然语言 prompt
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
    model_images: list[str] | None = None,
    user_requirement: str = "",
    reasoning_effort: str | None = None,
):
    """流式为单个方案生成最终 prompt（yield {"type": "thinking"|"content", "text": "..."}）。"""
    from wellflow.app.config import settings

    pool = get_model_pool()
    effort = reasoning_effort if reasoning_effort is not None else settings.llm_reasoning_effort

    system_prompt = GENERATE_IMAGE_PROMPT

    # user message：把 12 维方案 + 识别报告 + 用户需求 喂给 VLM
    import json as _json
    scheme_index = scheme.get("scheme_index", "?")
    scheme_name = scheme.get("scheme_name", f"方案{scheme_index}")
    user_text_parts = [
        f"【目标方案】方案 #{scheme_index} · {scheme_name}",
        f"方案完整 JSON：\n{_json.dumps(scheme, ensure_ascii=False, indent=2)}",
    ]
    if product_insight:
        user_text_parts.append(f"【商品识别报告】\n{product_insight}")
    if user_requirement:
        user_text_parts.append(f"【用户原始需求】\n{user_requirement}")

    product_images = product_images or []
    model_images = model_images or []

    # 显式告诉 VLM 图片编号语义（仅在有图时附加）
    n_prod = len(product_images)
    n_model = len(model_images)
    if n_prod or n_model:
        ref_lines = [f"共 {n_prod + n_model} 张参考图，编号含义如下："]
        if n_prod:
            ref_lines.append(f"• 第 1 ~ {n_prod} 张（共 {n_prod} 张）= 商品图（服装外观、颜色、细节、材质）")
        if n_model:
            ref_lines.append(f"• 第 {n_prod + 1} ~ {n_prod + n_model} 张（共 {n_model} 张）= 模特图（人脸、体型、气质）")
        user_text_parts.append("【参考图编号说明】\n" + "\n".join(ref_lines))

    # ⚠️ 有模特图时追加强制约束：确保 prompt 中的模特描述与参考图一致
    if n_model:
        user_text_parts.append(
            "【模特参考图约束】\n"
            "参考图中的模特是最终生图的人脸/体型/气质基准，必须严格参照模特图的特征，"
            "确保输出的 14 维 JSON 中 model 字段（性别、年龄、脸型、五官、肤色、发型、体型、气质）"
            "与参考图中的模特高度一致，不要自行替换或臆造模特特征。"
        )

    user_text_parts.append(
        f"请按 System Prompt 的 14 维结构输出 JSON。"
    )

    all_images = product_images + model_images

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
    model_images: list[str] | None = None,
    user_requirement: str = "",
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """为单个方案生成最终 prompt。

    Returns:
        {"prompt": str, "negative_prompt": str | None, "prompt_detail": dict,
         "raw_text": str, "thinking_text": str, "scheme_index": int | None}
    """
    from wellflow.app.config import settings

    pool = get_model_pool()
    effort = reasoning_effort if reasoning_effort is not None else settings.llm_reasoning_effort

    system_prompt = GENERATE_IMAGE_PROMPT

    import json as _json
    scheme_index = scheme.get("scheme_index")
    scheme_name = scheme.get("scheme_name", f"方案{scheme_index}")
    user_text_parts = [
        f"【目标方案】方案 #{scheme_index} · {scheme_name}",
        f"方案完整 JSON：\n{_json.dumps(scheme, ensure_ascii=False, indent=2)}",
    ]
    if product_insight:
        user_text_parts.append(f"【商品识别报告】\n{product_insight}")
    if user_requirement:
        user_text_parts.append(f"【用户原始需求】\n{user_requirement}")

    product_images = product_images or []
    model_images = model_images or []

    # 显式告诉 VLM 图片编号语义（仅在有图时附加）
    n_prod = len(product_images)
    n_model = len(model_images)
    if n_prod or n_model:
        ref_lines = [f"共 {n_prod + n_model} 张参考图，编号含义如下："]
        if n_prod:
            ref_lines.append(f"• 第 1 ~ {n_prod} 张（共 {n_prod} 张）= 商品图（服装外观、颜色、细节、材质）")
        if n_model:
            ref_lines.append(f"• 第 {n_prod + 1} ~ {n_prod + n_model} 张（共 {n_model} 张）= 模特图（人脸、体型、气质）")
        user_text_parts.append("【参考图编号说明】\n" + "\n".join(ref_lines))

    # ⚠️ 有模特图时追加强制约束：确保 prompt 中的模特描述与参考图一致
    if n_model:
        user_text_parts.append(
            "【模特参考图约束】\n"
            "参考图中的模特是最终生图的人脸/体型/气质基准，必须严格参照模特图的特征，"
            "确保输出的 14 维 JSON 中 model 字段（性别、年龄、脸型、五官、肤色、发型、体型、气质）"
            "与参考图中的模特高度一致，不要自行替换或臆造模特特征。"
        )

    user_text_parts.append("请按 System Prompt 的 14 维结构输出 JSON。")

    all_images = product_images + model_images

    print(f"[prompt_generation] 调用: scheme #{scheme_index}, "
          f"product_images={n_prod}, model_images={n_model}, "
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
    print(f"[prompt_generation] scheme #{scheme_index}: 原始文本 len={len(raw)}, "
          f"thinking={len(thinking_text) if thinking_text else 0}", flush=True)

    detail = _extract_json(raw)
    prompt, negative_prompt = _json_to_natural_prompt(detail)

    print(f"[prompt_generation] ✅ scheme #{scheme_index}: prompt len={len(prompt)}, "
          f"negative_prompt={'有' if negative_prompt else '无'}", flush=True)

    return {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "prompt_detail": detail,
        "raw_text": raw,
        "thinking_text": thinking_text,
        "scheme_index": scheme_index,
        "scheme_name": scheme_name,
    }
