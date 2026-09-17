"""Node 3 子图：PromptGeneration — 为每套选中的商拍方案循环调 VLM 生成最终生图 prompt。

输入：C2 interrupt 用户选了几套方案（selected_scheme_indices）。
输出：generate_prompts 列表（每个选中方案 → 1 个自然语言 prompt 字符串）
      + prompts_detail（每个 prompt 的 14 维 JSON 结构，前端展示）。

策略：顺序循环 N 次，每次一套方案。每套方案内部用流式，
      前端可以逐个看到每套的 prompt 生成过程。
"""

from __future__ import annotations

import asyncio
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


async def _gen_prompts(state: dict[str, Any]) -> dict[str, Any]:
    """为选中的 N 套方案逐个生成最终 prompt（流式，每个方案可独立推送 SSE）。"""
    from wellflow.app.nodes import prompt_generation as _pg
    from wellflow.app.event_bus import publish

    node1 = state.get("node1", {})
    node2 = state.get("node2", {})
    node3 = state.get("node3", {})
    req = state.get("request", {})
    task_id = state.get("task_id", "")

    product_insight = node1.get("product_insight", "")
    schemes: list[dict[str, Any]] = node2.get("schemes", [])
    selected_indices: list[int] = node2.get("selected_scheme_indices") or list(range(len(schemes)))
    user_requirement: str = req.get("user_requirement", "")

    # 模特图：C1 interrupt 时写入 node3.model_images
    model_image_paths: list[str] = node3.get("model_images") or []

    # 商品图：优先复用 Node1 缓存
    from wellflow.app.utils.image_store import paths_to_data_uris
    cached_product = node1.get("compressed_images") or []
    if cached_product:
        product_images = cached_product
    else:
        product_image_paths: list[str] = req.get("product_images") or []
        product_images = await asyncio.to_thread(paths_to_data_uris, product_image_paths)

    # 模特图：现场压缩（一般不大，to_thread 不阻塞 event loop）
    model_images = []
    if model_image_paths:
        model_images = await asyncio.to_thread(paths_to_data_uris, model_image_paths)

    # 过滤出选中的方案
    selected_schemes: list[dict[str, Any]] = [schemes[i] for i in selected_indices if 0 <= i < len(schemes)]

    if not selected_schemes:
        print("[node3] ⚠️ 没有选中的方案，跳过 prompt 生成", flush=True)
        new_node3: dict[str, Any] = {
            "model_images": model_image_paths,
            "generate_prompts": [],
            "prompts_detail": [],
        }
        return {"phase": "node3_prompt_gen", "node3": new_node3}

    if task_id:
        publish(task_id, "phase", {"phase": "node3_prompt_gen"})

    print(f"[node3] _gen_prompts 输入: 选中方案={selected_indices}, "
          f"共 {len(selected_schemes)} 套, "
          f"product_images={len(product_images)}, "
          f"model_images={len(model_images)}(paths={len(model_image_paths)})", flush=True)

    from wellflow.app.config import settings
    effort = settings.node3_reasoning_effort

    t_total = time.time()
    all_prompts: list[str] = []
    all_details: list[dict[str, Any]] = []
    all_think_parts: list[str] = []  # 💭 累积所有方案的 thinking 文本

    # ---- 顺序循环 N 次，每次一套方案 ----
    for si, scheme in enumerate(selected_schemes):
        scheme_index = scheme.get("scheme_index", si)
        scheme_name = scheme.get("scheme_name", f"方案{scheme_index}")

        if task_id:
            publish(task_id, "phase", {
                "phase": "node3_prompt_gen",
                "scheme_index": scheme_index,
                "scheme_name": scheme_name,
                "progress": f"生成第 {si+1}/{len(selected_schemes)} 套方案的 prompt...",
            })

        # ---- effort=low 用非流式，否则流式 ----
        t0 = time.time()
        use_non_stream = (effort == "low")
        chunk_index = 0

        if use_non_stream:
            # 非流式：一次拿到完整结果
            result = await _pg.generate_prompt_for_scheme(
                scheme=scheme,
                product_insight=product_insight,
                product_images=product_images,
                model_images=model_images or None,
                user_requirement=user_requirement,
                reasoning_effort=effort,
            )
            raw_text = result.get("raw_text", "")
            if task_id:
                publish(task_id, "prompt_chunk", {
                    "scheme_index": scheme_index,
                    "scheme_name": scheme_name,
                    "chunk": raw_text,
                    "index": 1,
                    "node": "node3",
                })
                publish(task_id, "prompt_chunk_done", {
                    "scheme_index": scheme_index,
                    "scheme_name": scheme_name,
                    "total_chunks": 1,
                    "node": "node3",
                })
            prompt_text = result.get("prompt", "")
            all_prompts.append(prompt_text)
            all_details.append({
                "scheme_index": scheme_index,
                "scheme_name": scheme_name,
                "prompt": prompt_text,
                "negative_prompt": result.get("negative_prompt"),
                "prompt_detail": result.get("prompt_detail"),
                "elapsed": round(time.time() - t0, 1),
            })
            # 💭 收集 thinking 文本（非流式路径）
            _nt = result.get("thinking_text")
            if _nt:
                all_think_parts.append(f"【方案 #{scheme_index} {scheme_name}】\n{_nt}")
        else:
            # 流式：逐 token 推送 SSE
            content_parts: list[str] = []
            think_parts: list[str] = []

            async for item in _pg.stream_generate_prompt(
                scheme=scheme,
                product_insight=product_insight,
                product_images=product_images,
                model_images=model_images or None,
                user_requirement=user_requirement,
                reasoning_effort=effort,
            ):
                if not item:
                    continue
                if isinstance(item, dict):
                    item_type = item.get("type", "content")
                    text = item.get("text", "")
                else:
                    item_type = "content"
                    text = item

                if not text:
                    continue

                if item_type == "thinking":
                    think_parts.append(text)
                    if task_id:
                        publish(task_id, "thinking_chunk", {
                            "chunk": text,
                            "index": len(think_parts),
                            "scheme_index": scheme_index,
                            "node": "node3",
                        })
                else:
                    content_parts.append(text)
                    chunk_index += 1
                    if task_id:
                        publish(task_id, "prompt_chunk", {
                            "chunk": text,
                            "index": chunk_index,
                            "scheme_index": scheme_index,
                            "scheme_name": scheme_name,
                            "node": "node3",
                        })

            # 流式结束，解析 JSON → 转自然语言 prompt
            raw_content = "".join(content_parts)
            detail = _pg._extract_json(raw_content)
            prompt_text, negative_prompt = _pg._json_to_natural_prompt(detail)

            if task_id:
                publish(task_id, "prompt_chunk_done", {
                    "scheme_index": scheme_index,
                    "scheme_name": scheme_name,
                    "total_chunks": chunk_index,
                    "node": "node3",
                })

            all_prompts.append(prompt_text)
            all_details.append({
                "scheme_index": scheme_index,
                "scheme_name": scheme_name,
                "prompt": prompt_text,
                "negative_prompt": negative_prompt,
                "prompt_detail": detail,
                "elapsed": round(time.time() - t0, 1),
            })
            # 💭 收集 thinking 文本（流式路径）
            if think_parts:
                all_think_parts.append(
                    f"【方案 #{scheme_index} {scheme_name}】\n{''.join(think_parts)}"
                )

        print(f"[node3] ✅ 方案 #{scheme_index}({scheme_name}) prompt 生成完成 — "
              f"耗时={time.time() - t0:.1f}s", flush=True)

    total_t = time.time() - t_total
    print(f"[node3] _gen_prompts 完成: {len(all_prompts)} 个 prompt, "
          f"总耗时={total_t:.1f}s", flush=True)

    # 用全新 dict 返回
    new_node3: dict[str, Any] = {
        "model_images": model_image_paths,
        "generate_prompts": all_prompts,
        "prompts_detail": all_details,
        "prompt_raw": "\n---\n".join(d.get("prompt", "") for d in all_details),
        "thinking_text": "\n\n".join(all_think_parts),
    }

    output = {"phase": "node3_prompt_gen", "node3": new_node3}
    print(f"[node3] _gen_prompts 输出 node3 keys={list(new_node3.keys())}", flush=True)
    return output
