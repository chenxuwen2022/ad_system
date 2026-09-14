"""Node 1 子图：input_analyzer → VLM 识别 → END。

流式/非流式策略（根据 reasoning_effort 预先选择，不做运行时降级）：
  - effort == "low"        → 非流式 analyze_product
  - effort == None / "none" / "medium" / "high" → 流式 stream_analyze_product
流式路径下每个 delta token 立即 publish 到 SSE event_bus，前端实时逐字输出。
"""

from __future__ import annotations

import time
from typing import Any

from wellflow.app.nodes import input_analyzer


def build_graph():
    """构建 Node 1 LangGraph 子图。需要 langgraph 已安装。"""
    try:
        from langgraph.graph import StateGraph, START, END
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "langgraph 未安装。请 pip install langgraph langgraph-checkpoint-postgres"
        ) from exc

    from wellflow.app.workflows.state import TaskState

    graph = StateGraph(TaskState)

    # 合并成一个节点：减少 checkpoint DB round-trip
    graph.add_node("do_analyze", _do_streaming_analyze)

    graph.add_edge(START, "do_analyze")
    graph.add_edge("do_analyze", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# 节点实现
# ---------------------------------------------------------------------------


async def _do_streaming_analyze(state: dict[str, Any]) -> dict[str, Any]:
    """一步完成 Node 1 全部工作：输入校验 → VLM 流式识别 → 报告汇总。

    关键：VLM 用 stream_chat_with_images，每个 token delta 立即 publish SSE，
    前端 TTFB = LLM 首 token 延迟（通常 < 1s），而不是等完整报告生成完。
    """
    from wellflow.app.nodes import product_analyzer
    from wellflow.app.event_bus import publish

    task_id = state.get("task_id", "")
    req = state.get("request", {})
    image_paths: list[str] = req.get("product_images") or []
    user_text: str = req.get("description", "")

    # --- Step 1: 输入校验（同步，轻量） ---
    analysis = input_analyzer.analyze_input(
        has_images=bool(req.get("has_images", False)),
        has_text=bool(user_text),
        image_count=int(req.get("image_count", 0)),
    )
    # 推第一个 phase：输入检查完成
    publish(task_id, "phase", {"phase": "node1_input_check"})

    # --- Step 2: 路径 → data URI（在 LLM 调用前才转，不存回 state） ---
    # Pillow 压缩是 CPU 密集 + 磁盘 I/O，用 to_thread 避免阻塞 asyncio event loop
    import asyncio
    from wellflow.app.utils.image_store import paths_to_data_uris
    images = await asyncio.to_thread(paths_to_data_uris, image_paths)
    print(f"[node1] 📥 VLM 输入: product_images paths={len(image_paths)} → data_uris={len(images)}, "
          f"text_len={len(user_text)}", flush=True)

    # --- Step 3: 推 phase = 调用 VLM ---
    publish(task_id, "phase", {"phase": "node1_vlm_analyzing"})

    # --- Step 4: 根据 reasoning_effort 预先选择流式/非流式 ---
    #   low → 非流式（避免 gateway 不兼容流式 + 低推理的情况）
    #   none / medium / high → 流式（前端逐字输出体验更好）
    from wellflow.app.config import settings
    effort = settings.llm_reasoning_effort
    use_non_stream = (effort == "low")

    t0 = time.time()
    full_report_parts: list[str] = []
    think_parts: list[str] = []
    content_chunk_index = 0
    think_chunk_index = 0
    first_content_ts = None
    first_think_ts = None

    if use_non_stream:
        # ---- 非流式路径（reasoning_effort=low）----
        print(f"[node1] 📌 reasoning_effort={effort} → 使用非流式 VLM", flush=True)
        full_report = await product_analyzer.analyze_product(
            images=images,
            user_text=user_text,
            reasoning_effort=effort,
        )
        full_report_parts.append(full_report)
        content_chunk_index = 1
        publish(task_id, "report_chunk", {"chunk": full_report, "index": 1, "node": "node1"})
    else:
        # ---- 流式路径（reasoning_effort=none/medium/high）----
        print(f"[node1] 📌 reasoning_effort={effort} → 使用流式 VLM", flush=True)
        async for item in product_analyzer.stream_analyze_product(
            images=images,
            user_text=user_text,
            reasoning_effort=effort,
        ):
            if not item:
                continue
            # 新版: item = {"type": "thinking"|"content", "text": "..."}
            # 兼容旧版: item 直接是 str
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
                    print(f"[node1] 💭 首 thinking token 到达 TTFB={first_think_ts - t0:.2f}s", flush=True)
                think_parts.append(text)
                think_chunk_index += 1
                publish(task_id, "thinking_chunk", {"chunk": text, "index": think_chunk_index, "node": "node1"})
            else:
                if first_content_ts is None:
                    first_content_ts = time.time()
                    print(f"[node1] 🟢 首 content token TTFB={first_content_ts - t0:.2f}s"
                          + (f" (thinking 耗时={first_content_ts - first_think_ts:.2f}s)" if first_think_ts else "")
                          , flush=True)
                full_report_parts.append(text)
                content_chunk_index += 1
                publish(task_id, "report_chunk", {"chunk": text, "index": content_chunk_index, "node": "node1"})

    full_report = "".join(full_report_parts)
    full_thinking = "".join(think_parts)
    total_ts = time.time()
    print(f"[node1] ✅ VLM 完成: 报告 {len(full_report)} 字, "
          f"thinking {len(full_thinking)} 字, "
          f"content_chunks={content_chunk_index}, think_chunks={think_chunk_index}, "
          f"总耗时={total_ts - t0:.2f}s"
          + (f", 首think={first_think_ts - t0:.2f}s" if first_think_ts else "")
          + (f", 首content={first_content_ts - t0:.2f}s" if first_content_ts else "")
          , flush=True)

    # 推 done（带上 thinking 汇总，方便前端展示）
    publish(task_id, "report_chunk_done", {
        "total_chunks": content_chunk_index,
        "thinking_total_chunks": think_chunk_index,
        "thinking_text": full_thinking if full_thinking else None,
        "node": "node1",
    })

    return {
        "phase": "node1_vlm_done",
        "node1": {
            **state.get("node1", {}),
            "input_analysis": analysis,
            "product_insight": full_report,
            # 🔁 缓存已压缩的商品图 data URIs，供 Node2 复用（避免重复 PIL 压缩 ~1.2s）
            "compressed_images": images,
        },
    }
