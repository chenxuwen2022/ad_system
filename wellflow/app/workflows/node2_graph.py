"""Node 2 子图：PlanningScheme — VLM 多模态流式产出多套商拍策划方案。

和 Node1 / Node3 保持一致：统一流式 + thinking=low（逐 token 推 SSE）。
输入：商品识别报告（Node1）+ 商品图 data URI（Node1 缓存）。
输出写入 state.node2（SchemeState），每套以 report_text 供 C2 和 Node3 使用。
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
    """VLM 流式产出配置数量的完整方案，结束时拆分并推送方案列表。

    Node2 **不使用模特图**（模特图只在 Node3 prompt 生成阶段才喂给 VLM），
    只吃 Node1 的商品图缓存 + product_insight + 用户创作需求。
    输出写入 state.node2（SchemeState），保留 C2 选择所需的最小载体。
    """
    import asyncio
    from wellflow.app.nodes import planning_scheme as _ps
    from wellflow.app.event_bus import publish

    node1 = state.get("node1", {})
    req = state.get("request", {})
    task_id = state.get("task_id", "")

    product_insight = node1.get("product_insight", "")
    user_requirement: str = req.get("user_requirement", "")
    if state.get("_redo_instruction"):
        user_requirement += "\n本次重新生成要求：" + state["_redo_instruction"]

    from wellflow.app.config import settings as _settings
    scheme_count = max(1, int(_settings.node2_scheme_count_default))
    if task_id:
        publish(task_id, "phase", {"phase": "node2_plan_scheme", "scheme_count": scheme_count, "reset_text": True})

    cached_product = node1.get("compressed_images") or []
    product_image_paths: list[str] = req.get("product_images") or []
    product_images = cached_product
    if not product_images and product_image_paths:
        from wellflow.app.utils.image_store import paths_to_data_uris
        product_images = await asyncio.to_thread(paths_to_data_uris, product_image_paths)

    print(f"[node2] _plan_schemes 输入: user_requirement={'有' if user_requirement else '无'}, "
          f"product_images={len(product_images)}", flush=True)

    # reasoning_effort 从 settings.node2_reasoning_effort 读取（默认 low，可配）
    effort = _settings.node2_reasoning_effort

    t0 = time.time()
    content_parts: list[str] = []
    think_parts: list[str] = []
    content_chunk_index = 0
    think_chunk_index = 0
    first_content_ts = None
    first_think_ts = None

    # ---- 开始流式 VLM ----
    print(f"[node2] 📌 reasoning_effort={effort} → 流式 stream_plan_schemes", flush=True)

    async for item in _ps.stream_plan_schemes(
        product_insight=product_insight,
        product_images=product_images or None,
        user_requirement=user_requirement,
        scheme_count=scheme_count,
        reasoning_effort=effort,
        task_id=task_id,
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

    # 首次和重新生成均严格校验 JSON 数组数量；正文保持 Markdown。
    schemes = _ps.split_scheme_reports(raw_text, expected_count=scheme_count)

    total_ts = time.time()
    print(f"[node2] ✅ 流式 VLM 完成: schemes={len(schemes)} 套, "
          f"raw={len(raw_text)} 字, thinking={len(full_thinking)} 字, "
          f"content_chunks={content_chunk_index}, think_chunks={think_chunk_index}, "
          f"总耗时={total_ts - t0:.2f}s"
          + (f", 首think={first_think_ts - t0:.2f}s" if first_think_ts else "")
          + (f", 首content={first_content_ts - t0:.2f}s" if first_content_ts else "")
          , flush=True)

    # 推送完整文本报告；载体结构只用于现有 C2 确认流程。
    if task_id:
        publish(task_id, "scheme_chunk_done", {
            "total_chunks": content_chunk_index,
            "thinking_total_chunks": think_chunk_index,
            "thinking_text": full_thinking if full_thinking else None,
            "node": "node2",
            "scheme_count": len(schemes),
            "schemes": schemes,
            "_final": True,  # 标记这是最后一条汇总事件，和逐条 done 区分
        })

    new_node2: dict[str, Any] = {
        "schemes": schemes,
        "scheme_raw": raw_text,
        "selected_scheme_indices": [],
        "thinking_text": full_thinking if full_thinking else "",
    }

    output = {"_redo_target": None, "_redo_instruction": None, "phase": "node2_plan_scheme", "node2": new_node2}
    print(f"[node2] _plan_schemes 输出: {len(schemes)} 套商拍方案", flush=True)
    return output
