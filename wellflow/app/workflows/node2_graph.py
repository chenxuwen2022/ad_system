"""Node 2 子图：PlanningScheme — VLM 多模态一次流式产出 N 套 12 维商拍方案。

和 Node1 / Node3 保持一致：统一流式 + thinking=low（逐 token 推 SSE）。
输入：商品识别报告（Node1）+ 商品图 data URI（Node1 缓存）+ 可选模特图（C1 confirm 上传）
输出写入 state.node2（SchemeState），保持和旧版完全一致的 shape。
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
    """VLM 流式产出 1 套最终商拍方案，逐 token 推送 SSE。

    复用 Node1 的流式模式：thinking_chunk / scheme_chunk / scheme_chunk_done，
    Node2 **不使用模特图**（模特图只在 Node3 prompt 生成阶段才喂给 VLM），
    只吃 Node1 的商品图缓存 + product_insight + 用户创作需求。
    输出写入 state.node2（SchemeState），保持和旧版完全一致的 shape。
    """
    import asyncio
    from wellflow.app.nodes import planning_scheme as _ps
    from wellflow.app.event_bus import publish

    node1 = state.get("node1", {})
    req = state.get("request", {})
    task_id = state.get("task_id", "")

    product_insight = node1.get("product_insight", "")
    user_requirement: str = req.get("user_requirement", "")

    # 方案数量由 settings.node2_scheme_count_default 驱动（固定=1）
    from wellflow.app.config import settings as _settings
    scheme_count: int = int(req.get("scheme_count") or _settings.node2_scheme_count_default)

    if task_id:
        publish(task_id, "phase", {"phase": "node2_plan_scheme"})

    # 🔑 商品图：优先复用 Node1 压缩缓存（data URI），没有则从原始路径压缩
    cached_product = node1.get("compressed_images") or []
    product_image_paths: list[str] = req.get("product_images") or []
    product_images = cached_product
    if not product_images and product_image_paths:
        from wellflow.app.utils.image_store import paths_to_data_uris
        product_images = await asyncio.to_thread(paths_to_data_uris, product_image_paths)

    print(f"[node2] _plan_schemes 输入: scheme_count={scheme_count}, "
          f"user_requirement={'有' if user_requirement else '无'}, "
          f"product_images={len(product_images)}", flush=True)

    # 统一 low —— Node1/Node2/Node3 默认 low，保持 thinking 开启但推理成本可控
    from wellflow.app.config import settings
    effort = "low"

    t0 = time.time()
    content_parts: list[str] = []
    think_parts: list[str] = []
    content_chunk_index = 0
    think_chunk_index = 0
    first_content_ts = None
    first_think_ts = None

    print(f"[node2] 📌 reasoning_effort={effort} → 流式 stream_plan_schemes", flush=True)

    async for item in _ps.stream_plan_schemes(
        product_insight=product_insight,
        product_images=product_images or None,
        user_requirement=user_requirement,
        scheme_count=scheme_count,
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
            if first_think_ts is None:
                first_think_ts = time.time()
                print(f"[node2] 💭 首 thinking token TTFB={first_think_ts - t0:.2f}s", flush=True)
            think_parts.append(text)
            think_chunk_index += 1
            if task_id:
                publish(task_id, "thinking_chunk", {"chunk": text, "index": think_chunk_index, "node": "node2"})
        else:
            if first_content_ts is None:
                first_content_ts = time.time()
                print(f"[node2] 🟢 首 content token TTFB={first_content_ts - t0:.2f}s"
                      + (f" (thinking 耗时={first_content_ts - first_think_ts:.2f}s)" if first_think_ts else "")
                      , flush=True)
            content_parts.append(text)
            content_chunk_index += 1
            if task_id:
                publish(task_id, "scheme_chunk", {"chunk": text, "index": content_chunk_index, "node": "node2"})

    raw_text = "".join(content_parts)
    full_thinking = "".join(think_parts)

    # 解析 JSON —— 复用 planning_scheme 的多层兜底
    parsed = _ps._extract_json(raw_text)
    schemes = parsed.get("schemes", [])
    if not isinstance(schemes, list):
        print(f"[node2] ⚠️ schemes 不是 list，实际是 {type(schemes)}", flush=True)
        schemes = []

    # 兜底：按 scheme_count 截断 / 补空
    if len(schemes) > scheme_count:
        schemes = schemes[:scheme_count]
    elif len(schemes) < scheme_count and schemes:
        print(f"[node2] ⚠️ VLM 只返回 {len(schemes)}/{scheme_count} 套，补齐空方案", flush=True)
        schemes.extend([{"scheme_index": len(schemes), "scheme_name": "方案待补充", "_placeholder": True}] * (scheme_count - len(schemes)))

    # VLM 完全没返回方案 → 必须抛错，否则会静默写 [] 覆盖 state 导致 C2 无方案可选
    if not schemes:
        raise RuntimeError(
            f"[node2] VLM 未返回任何方案（raw_text len={len(raw_text)}），请重试"
        )

    total_ts = time.time()
    print(f"[node2] ✅ 流式 VLM 完成: schemes={len(schemes)} 套, "
          f"raw={len(raw_text)} 字, thinking={len(full_thinking)} 字, "
          f"content_chunks={content_chunk_index}, think_chunks={think_chunk_index}, "
          f"总耗时={total_ts - t0:.2f}s"
          + (f", 首think={first_think_ts - t0:.2f}s" if first_think_ts else "")
          + (f", 首content={first_content_ts - t0:.2f}s" if first_content_ts else "")
          , flush=True)

    # 推 done（带上完整 schemes，前端 C2 阶段直接消费）
    if task_id:
        publish(task_id, "scheme_chunk_done", {
            "total_chunks": content_chunk_index,
            "thinking_total_chunks": think_chunk_index,
            "thinking_text": full_thinking if full_thinking else None,
            "node": "node2",
            "scheme_count": len(schemes),
            "schemes": schemes,
        })

    new_node2: dict[str, Any] = {
        "schemes": schemes,
        "scheme_raw": raw_text,
        "selected_scheme_indices": [],
        "thinking_text": full_thinking if full_thinking else "",
        "base_scheme_count": len(schemes),  # 🛑 锚点：Node2 正向产出的原始方案数
    }

    output = {"phase": "node2_plan_scheme", "node2": new_node2}
    print(f"[node2] _plan_schemes 输出: schemes={len(schemes)} 套, base_scheme_count={len(schemes)}", flush=True)
    return output
