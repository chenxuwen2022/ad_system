"""Node 2 子图：PlanningScheme — VLM 多模态一次产出 N 套结构化商拍方案。

流式/非流式策略（和 Node1 一致）：
  - effort == "low"        → 非流式 plan_schemes()（强制 JSON，response_format=json_object）
  - effort == None / "none" / "medium" / "high" → 流式 stream_plan_schemes()
三套方案要求在方案定位、视觉主题、场景设定、模特气质、光影风格上有显著差异。

输出写入 state.node2（SchemeState）。
"""

from __future__ import annotations

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

    graph.add_node("planning_scheme", _plan_schemes)
    graph.add_edge(START, "planning_scheme")
    graph.add_edge("planning_scheme", END)

    return graph.compile()


async def _plan_schemes(state: dict[str, Any]) -> dict[str, Any]:
    """VLM 一次产出 N 套 12 维商拍方案（非流式 or 流式，根据 reasoning_effort）。"""
    import asyncio
    from wellflow.app.nodes import planning_scheme as _ps
    from wellflow.app.event_bus import publish

    node1 = state.get("node1", {})
    node3 = state.get("node3", {})
    req = state.get("request", {})
    task_id = state.get("task_id", "")

    product_insight = node1.get("product_insight", "")
    product_image_paths: list[str] = req.get("product_images") or []
    user_requirement: str = req.get("user_requirement", "")
    model_image_paths: list[str] = node3.get("model_images") or []

    # 默认生成 3 套方案，可通过 request.scheme_count 覆盖
    scheme_count: int = int(req.get("scheme_count") or 3)

    if task_id:
        publish(task_id, "phase", {"phase": "node2_plan_scheme"})

    # 🔑 复用 Node1 已缓存的压缩结果，跳过重复 PIL
    from wellflow.app.utils.image_store import paths_to_data_uris
    cached = node1.get("compressed_images") or []
    if cached:
        print(f"[node2] 🔁 复用 Node1 已压缩的 {len(cached)} 张商品图", flush=True)
        product_images = cached
    else:
        product_images = await asyncio.to_thread(paths_to_data_uris, product_image_paths)
        print(f"[node2] 🔨 Node1 无缓存，现场压缩 {len(product_image_paths)} 张商品图", flush=True)

    # 模特图转 data URI（C1 阶段已落盘，state.node3.model_images 是文件路径）
    if model_image_paths:
        model_images = await asyncio.to_thread(paths_to_data_uris, model_image_paths)
        print(f"[node2] 🧍 模特图 {len(model_image_paths)} 张已转 data URI", flush=True)
    else:
        model_images = None

    print(f"[node2] _plan_schemes 输入: scheme_count={scheme_count}, "
          f"product_images={len(product_images)}(paths={len(product_image_paths)}), "
          f"model_images={len(model_images) if model_images else 0}(paths={len(model_image_paths)}), "
          f"user_requirement={'有' if user_requirement else '无'}", flush=True)

    # 根据 reasoning_effort 选流式/非流式
    from wellflow.app.config import settings
    effort = settings.node2_reasoning_effort
    use_non_stream = (effort == "low")

    t0 = time.time()
    content_parts: list[str] = []
    think_parts: list[str] = []
    content_chunk_index = 0
    think_chunk_index = 0
    first_content_ts = None
    first_think_ts = None

    result: dict[str, Any] | None = None

    if use_non_stream:
        # ---- 非流式 ----
        print(f"[node2] 📌 reasoning_effort={effort} → 非流式 plan_schemes()", flush=True)
        result = await _ps.plan_schemes(
            product_insight=product_insight,
            product_images=product_images,
            user_requirement=user_requirement,
            scheme_count=scheme_count,
            reasoning_effort=effort,
            model_images=model_images,
        )
        content_chunk_index = 1
        raw_text = result.get("raw_text", "")
        thinking_text = result.get("thinking_text")

        # 🔍 非流式路径诊断
        print(f"[node2] 🔍 非流式 raw_text 前 500 字: {raw_text[:500]}", flush=True)
        print(f"[node2] 🔍 非流式 result keys={list(result.keys())}, "
              f"schemes count={len(result.get('schemes', []))}", flush=True)

        if task_id and thinking_text:
            publish(task_id, "thinking_chunk", {"chunk": thinking_text, "index": 1, "node": "node2"})
        if task_id:
            publish(task_id, "scheme_chunk", {"chunk": raw_text, "index": 1, "node": "node2"})
            publish(task_id, "scheme_chunk_done", {
                "total_chunks": 1,
                "thinking_total_chunks": 1 if thinking_text else 0,
                "thinking_text": thinking_text,
                "node": "node2",
                "scheme_count": len(result.get("schemes", [])),
            })
    else:
        # ---- 流式 ----
        print(f"[node2] 📌 reasoning_effort={effort} → 流式 stream_plan_schemes()", flush=True)
        async for item in _ps.stream_plan_schemes(
            product_insight=product_insight,
            product_images=product_images,
            user_requirement=user_requirement,
            scheme_count=scheme_count,
            reasoning_effort=effort,
            model_images=model_images,
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
                if first_think_ts is None:
                    first_think_ts = time.time()
                    print(f"[node2] 💭 首 thinking token TTFB={first_think_ts - t0:.2f}s", flush=True)
                think_parts.append(text)
                think_chunk_index += 1
                publish(task_id, "thinking_chunk", {"chunk": text, "index": think_chunk_index, "node": "node2"})
            else:
                if first_content_ts is None:
                    first_content_ts = time.time()
                    print(f"[node2] 🟢 首 content token TTFB={first_content_ts - t0:.2f}s"
                          + (f" (thinking 耗时={first_content_ts - first_think_ts:.2f}s)" if first_think_ts else "")
                          , flush=True)
                content_parts.append(text)
                content_chunk_index += 1
                publish(task_id, "scheme_chunk", {"chunk": text, "index": content_chunk_index, "node": "node2"})

        # 流式结束，解析 JSON
        raw_content = "".join(content_parts)
        print(f"[node2] 🎬 流式 VLM 完成: content len={len(raw_content)}, "
              f"thinking len={len(''.join(think_parts))}, 总耗时={time.time() - t0:.2f}s", flush=True)

        # 🔍 诊断：打印原始输出前 800 字，确认 gpt-5.6-sol 输出格式
        print(f"[node2] 🔍 raw_content 前 800 字:\n{raw_content[:800]}", flush=True)
        print(f"[node2] 🔍 raw_content 后 200 字:\n{raw_content[-200:]}", flush=True)

        parsed = _ps._extract_json(raw_content)
        print(f"[node2] 🔍 _extract_json 解析结果: keys={list(parsed.keys())}, "
              f"type(schemes)={type(parsed.get('schemes')).__name__}", flush=True)

        schemes = parsed.get("schemes", [])

        if not isinstance(schemes, list):
            print(f"[node2] ⚠️ schemes 不是 list，实际是 {type(schemes)}", flush=True)
            schemes = []

        if len(schemes) > scheme_count:
            schemes = schemes[:scheme_count]
        elif len(schemes) < scheme_count and schemes:
            print(f"[node2] ⚠️ VLM 只返回 {len(schemes)}/{scheme_count} 套，补齐空方案", flush=True)
            schemes.extend([
                {"scheme_index": len(schemes), "scheme_name": "方案待补充", "_placeholder": True}
            ] * (scheme_count - len(schemes)))

        result = {
            "schemes": schemes,
            "raw_text": raw_content,
        }

    # 推 done
    if task_id:
        publish(task_id, "scheme_chunk_done", {
            "total_chunks": content_chunk_index,
            "thinking_total_chunks": think_chunk_index,
            "thinking_text": "".join(think_parts) if think_parts else None,
            "node": "node2",
            "scheme_count": len(result.get("schemes", [])),
        })

    schemes = result.get("schemes", [])
    raw_text = result.get("raw_text", "")

    # 用全新 dict 返回，避免 merge 丢失
    new_node2: dict[str, Any] = {
        "schemes": schemes,
        "scheme_raw": raw_text,
        "selected_scheme_indices": [],  # C2 interrupt 后写入
    }

    output = {"phase": "node2_plan_scheme", "node2": new_node2}
    print(f"[node2] _plan_schemes 输出: schemes={len(schemes)} 套, 总耗时={time.time() - t0:.2f}s", flush=True)
    return output
