"""Node 3 子图：PromptGeneration — 为每套选中的商拍方案循环调 VLM 生成最终生图 prompt。

输入：C2 interrupt 用户选了几套方案（selected_scheme_indices）。
输出：generate_prompts 文本列表 + prompts_detail 展示元信息。

策略：每套选中方案调用一次 VLM，一次返回该方案全部提示词。
"""

from __future__ import annotations

from wellflow.app.logging import log_message

from wellflow.app.newapi.observability import business_operation

import asyncio
import json
import time
from typing import Any


def build_graph():
    try:
        from langgraph.graph import StateGraph, START, END
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "langgraph 未安装。请 pip install langgraph langgraph-checkpoint-postgres"
        ) from exc

    from wellflow.app.workflows.state import TaskState

    graph = StateGraph(TaskState)

    graph.add_node("prompt_generation", _gen_prompts)
    graph.add_edge(START, "prompt_generation")
    graph.add_edge("prompt_generation", END)

    return graph.compile()


@business_operation("Node3/提示词生成")
async def _gen_prompts(state: dict[str, Any]) -> dict[str, Any]:
    """每套选中方案调用一次模型，批量生成该方案的全部中文提示词。

    generate_prompts 最终长度 = sum(per_scheme_count)。
    """
    from wellflow.app.nodes import prompt_generation as _pg
    from wellflow.app.event_bus import publish

    node1 = state.get("node1", {})
    node2 = state.get("node2", {})
    node3 = state.get("node3", {})
    req = state.get("request", {})
    task_id = state.get("task_id", "")

    product_insight = node1.get("product_insight", "")
    schemes: list[dict[str, Any]] = node2.get("schemes", [])
    from wellflow.app.workflows.scheme_selection import validate_scheme_indices
    selected_indices = validate_scheme_indices(node2.get("selected_scheme_indices"), len(schemes))
    # 每套选中方案要生成几份 prompt —— 新链路由 C2 写入 node2.per_scheme_count
    per_scheme_count: list[int] = node2.get("per_scheme_count") or []
    user_requirement: str = req.get("user_requirement", "")
    if state.get("_redo_instruction"):
        user_requirement += "\n本次重新生成要求：" + state["_redo_instruction"]

    # 三类参考图：C2 interrupt 时写入 node3.reference_images
    ref_images: dict[str, list[str]] = node3.get("reference_images") or {}
    mannequin_paths: list[str] = ref_images.get("mannequin") or []
    scene_paths: list[str] = ref_images.get("scene") or []
    outfit_paths: list[str] = ref_images.get("outfit") or []

    # 商品图：优先复用 Node1 缓存
    from wellflow.app.utils.image_store import paths_to_data_uris
    cached_product = node1.get("compressed_images") or []
    if cached_product:
        product_images = cached_product
    else:
        product_image_paths: list[str] = req.get("product_images") or []
        product_images = await asyncio.to_thread(paths_to_data_uris, product_image_paths)

    # 三类参考图现场压缩
    mannequin_images = []
    if mannequin_paths:
        mannequin_images = await asyncio.to_thread(paths_to_data_uris, mannequin_paths)
    scene_images = []
    if scene_paths:
        scene_images = await asyncio.to_thread(paths_to_data_uris, scene_paths)
    outfit_images = []
    if outfit_paths:
        outfit_images = await asyncio.to_thread(paths_to_data_uris, outfit_paths)

    for label, paths, images in (
        ("模特图", mannequin_paths, mannequin_images),
        ("场景图", scene_paths, scene_images),
        ("穿搭图", outfit_paths, outfit_images),
    ):
        if len(paths) != len(images):
            raise ValueError(f"{label}读取不完整，请重新上传后再生成提示词")

    required_binding = _pg.reference_binding_block(product_images, {
        "mannequin": mannequin_images, "scene": scene_images, "outfit": outfit_images,
    })

    # 过滤出选中的方案
    selected_schemes: list[dict[str, Any]] = [schemes[i] for i in selected_indices if 0 <= i < len(schemes)]

    if (len(per_scheme_count) != len(selected_schemes)
            or any(type(c) is not int or c < 1 for c in per_scheme_count)):
        raise ValueError("Node3 缺少有效的前端 per_scheme_count")

    if not selected_schemes:
        raise ValueError("没有已确认的商拍方案，无法生成提示词")

    if task_id:
        publish(task_id, "phase", {"phase": "node3_prompt_gen"})

    total_prompts = sum(per_scheme_count)
    log_message(f"[node3] _gen_prompts 输入: 选中方案={selected_indices}, "
          f"per_scheme_count={per_scheme_count}, "
          f"共 {len(selected_schemes)} 套方案 → {total_prompts} 份 prompt, "
          f"product_images={len(product_images)}, "
          f"mannequin={len(mannequin_images)}(paths={len(mannequin_paths)}), "
          f"scene={len(scene_images)}(paths={len(scene_paths)}), "
          f"outfit={len(outfit_images)}(paths={len(outfit_paths)})", page='对话', business='node3生图提示词', status='记录')

    # reasoning_effort 从 settings.node3_reasoning_effort 读取（默认 low，可配）
    from wellflow.app.config import settings as _settings
    effort = _settings.node3_reasoning_effort

    t_total = time.time()
    all_prompts: list[str] = []
    all_details: list[dict[str, Any]] = []
    all_think_parts: list[str] = []  # 💭 累积所有方案的 thinking 文本

    content_chunk_index = 0
    # 每套方案仅调用一次模型，一次拿回该方案的全部提示词。
    for si, scheme in enumerate(selected_schemes):
        scheme_index = scheme.get("scheme_index", si)
        scheme_name = scheme.get("scheme_name", f"方案{scheme_index}")
        n_variants = per_scheme_count[si]
        if task_id:
            publish(task_id, "phase", {
                "phase": "node3_prompt_gen",
                "scheme_index": scheme_index,
                "scheme_name": scheme_name,
                "progress": f"正在为{scheme_name}一次生成 {n_variants} 份提示词",
            })

        started = time.time()
        content_parts: list[str] = []
        think_parts: list[str] = []
        async for item in _pg.stream_generate_prompt(
            scheme=scheme,
            product_insight=product_insight,
            product_images=product_images,
            reference_images={
                "mannequin": mannequin_images or [],
                "scene": scene_images or [],
                "outfit": outfit_images or [],
            },
            user_requirement=user_requirement,
            prompt_count=n_variants,
            reasoning_effort=effort,
            task_id=task_id,
        ):
            if not item:
                continue
            item_type = item.get("type", "content") if isinstance(item, dict) else "content"
            text = item.get("text", "") if isinstance(item, dict) else item
            if not text:
                continue
            if item_type == "thinking":
                think_parts.append(text)
                if task_id:
                    publish(task_id, "thinking_chunk", {
                        "chunk": text, "index": len(think_parts),
                        "scheme_index": scheme_index, "node": "node3",
                    })
            else:
                content_parts.append(text)
                content_chunk_index += 1
                if task_id:
                    publish(task_id, "prompt_chunk", {
                        "chunk": text, "index": content_chunk_index, "node": "node3",
                        "scheme_index": scheme_index, "scheme_name": scheme_name,
                        "variant_total": n_variants,
                    })

        raw_content = "".join(content_parts)
        generated = _pg.split_generated_prompts(
            raw_content, n_variants, required_binding=required_binding,
        )
        elapsed = round(time.time() - started, 1)
        scheme_details: list[dict[str, Any]] = []
        for vi, item in enumerate(generated):
            prompt_text = item["prompt"]
            prompt_name = f"{scheme_name} · {item['title']}"
            detail = {
                "scheme_index": scheme_index,
                "scheme_name": scheme_name,
                "prompt_name": prompt_name,
                "prompt_subtitle": item["title"],
                "variant_index": vi,
                "variant_total": n_variants,
                "prompt": prompt_text,
                "elapsed": elapsed,
            }
            all_prompts.append(prompt_text)
            all_details.append(detail)
            scheme_details.append(detail)
        if task_id:
            publish(task_id, "prompt_chunk_done", {
                "node": "node3", "scheme_index": scheme_index,
                "variant_total": n_variants,
                "generate_prompts": [item["prompt"] for item in generated],
                "prompts_detail": scheme_details,
                "_final": True,
            })

        if think_parts:
            all_think_parts.append(f"【方案 #{scheme_index} {scheme_name}】\n{''.join(think_parts)}")
        log_message(f"[node3] ✅ 方案 #{scheme_index}({scheme_name}) 一次调用生成 {n_variants} 份提示词 "
              f"— 耗时={elapsed:.1f}s", page='对话', business='node3生图提示词', status='成功')

    total_t = time.time() - t_total
    log_message(f"[node3] _gen_prompts 完成: {len(all_prompts)} 个 prompt, "
          f"总耗时={total_t:.1f}s", page='对话', business='node3生图提示词', status='记录')

    if not all_prompts or any(not prompt.strip() for prompt in all_prompts):
        raise RuntimeError("提示词生成返回空内容，请重试当前节点")

    # 用全新 dict 返回
    new_node3: dict[str, Any] = {
        "reference_images": ref_images,
        "generate_prompts": all_prompts,
        "prompts_detail": all_details,
        "prompt_raw": json.dumps([{"title": d["prompt_subtitle"], "prompt": d["prompt"]} for d in all_details], ensure_ascii=False),
        "thinking_text": "\n\n".join(all_think_parts),
    }

    output = {"_redo_target": None, "_redo_instruction": None, "phase": "node3_prompt_gen", "node3": new_node3}
    log_message(f"[node3] _gen_prompts 输出 node3 keys={list(new_node3.keys())}", page='对话', business='node3生图提示词', status='记录')
    return output
