"""Node 2 子图：PlanningAgent（VLM 多模态生成多屏服饰电商模特图 prompt）。

流式/非流式策略（根据 reasoning_effort 预先选择，不做运行时降级）：
  - effort == "low"        → 非流式 plan()（强制 JSON，response_format=json_object）
  - effort == None / "none" / "medium" / "high" → 流式 stream_plan()（自然输出 JSON，末尾解析）
流式路径下 thinking + content 每个 delta 立即 publish SSE，前端实时逐字输出。
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

    graph.add_node("planning_agent", _plan)
    graph.add_edge(START, "planning_agent")
    graph.add_edge("planning_agent", END)

    return graph.compile()


async def _plan(state: dict[str, Any]) -> dict[str, Any]:
    """VLM 流式多模态生成 N 屏服饰电商模特图 prompt。"""
    import asyncio
    from wellflow.app.nodes import planning_agent
    from wellflow.app.event_bus import publish

    node1 = state.get("node1", {})
    node2 = state.get("node2", {})
    req = state.get("request", {})
    task_id = state.get("task_id", "")

    product_insight = node1.get("product_insight", "")
    product_image_paths: list[str] = req.get("product_images") or []
    model_image_paths: list[str] = node2.get("model_images") or []
    ratio: str = node2.get("ratio") or "9:16"
    count: int = int(node2.get("count") or 3)

    # 🔑 先推 phase，前端立即看到 Node 2 启动状态
    if task_id:
        publish(task_id, "phase", {"phase": "node2_planning"})

    # 商品图：优先复用 Node1 已缓存的压缩结果，跳过重复 PIL（省 ~1.2s）
    from wellflow.app.utils.image_store import paths_to_data_uris
    cached = node1.get("compressed_images") or []
    if cached:
        print(f"[node2] 🔁 复用 Node1 已压缩的 {len(cached)} 张商品图（跳过 PIL）", flush=True)
        product_images = cached
    else:
        # 兜底：老任务没有缓存，现场压（PIL 放 to_thread，不阻塞 event loop）
        product_images = await asyncio.to_thread(paths_to_data_uris, product_image_paths)
        print(f"[node2] 🔨 Node1 无缓存，现场压缩 {len(product_image_paths)} 张商品图", flush=True)

    # 模特图（C1 resume 时刚上传的，Node1 没见过）：按 ≤6MB 规则现场压缩
    model_images = await asyncio.to_thread(paths_to_data_uris, model_image_paths) if model_image_paths else []

    print(f"[node2] _plan 输入: count={count}, ratio={ratio}, "
          f"product_images={len(product_images)}(paths={len(product_image_paths)}), "
          f"model_images={len(model_images)}(paths={len(model_image_paths)})",
          flush=True)

    # --- 根据 reasoning_effort 预先选择流式/非流式 ---
    #   low → 非流式 plan()（强制 JSON）
    #   none / medium / high → 流式 stream_plan()（自然输出 JSON）
    # ⚠️ Node2 用独立的 node2_reasoning_effort，默认 "low"，省掉 Deep Thinking
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
        # ---- 非流式路径（reasoning_effort=low）----
        print(f"[node2] 📌 reasoning_effort={effort} → 使用非流式 plan()", flush=True)
        result = await planning_agent.plan(
            product_insight=product_insight,
            product_images=product_images,
            model_images=model_images or None,
            count=count,
            reasoning_effort=effort,
        )
        full_text = result.get("raw_text", "")
        non_stream_thinking = result.get("thinking_text")
        content_chunk_index = 1

        # 如果非流式返回了 thinking_text，先发出来让前端有东西看
        if task_id and non_stream_thinking:
            publish(task_id, "thinking_chunk", {"chunk": non_stream_thinking, "index": 1, "node": "node2"})
        if task_id:
            publish(task_id, "report_chunk", {"chunk": full_text, "index": 1, "node": "node2"})
            publish(task_id, "report_chunk_done", {
                "total_chunks": 1,
                "thinking_total_chunks": 1 if non_stream_thinking else 0,
                "thinking_text": non_stream_thinking,
                "node": "node2",
            })
    else:
        # ---- 流式路径（reasoning_effort=none/medium/high）----
        print(f"[node2] 📌 reasoning_effort={effort} → 使用流式 stream_plan()", flush=True)
        async for item in planning_agent.stream_plan(
            product_insight=product_insight,
            product_images=product_images,
            model_images=model_images or None,
            count=count,
            reasoning_effort=effort,
        ):
            if not item:
                continue
            # item = {"type": "thinking"|"content", "text": "..."}
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
                publish(task_id, "report_chunk", {"chunk": text, "index": content_chunk_index, "node": "node2"})

        # 流式结束，解析 JSON
        raw_content = "".join(content_parts)
        print(f"[node2] 🎬 流式 VLM 完成: content len={len(raw_content)}, "
              f"thinking len={len(''.join(think_parts))}, "
              f"content_chunks={content_chunk_index}, think_chunks={think_chunk_index}, "
              f"总耗时={time.time() - t0:.2f}s", flush=True)

        parsed = planning_agent._extract_json(raw_content)
        prompts = parsed.get("generate_prompts", [])

        if not isinstance(prompts, list):
            print(f"[node2] ⚠️ generate_prompts 不是 list，实际是 {type(prompts)}", flush=True)
            prompts = []

        # 兜底：截断或补空
        if len(prompts) > count:
            prompts = prompts[:count]
        elif len(prompts) < count and prompts:
            print(f"[node2] ⚠️ VLM 只返回 {len(prompts)}/{count} 屏，补齐空 prompt", flush=True)
            prompts.extend([""] * (count - len(prompts)))

        prompts = [p for p in prompts if p and str(p).strip()]

        result = {
            "generate_prompts": prompts,
            "raw_text": raw_content,
        }
        print(f"[node2] ✅ 流式解析 generate_prompts={len(prompts)} 屏", flush=True)

    # 推 done（带上 thinking 汇总）
    if task_id:
        publish(task_id, "report_chunk_done", {
            "total_chunks": content_chunk_index,
            "thinking_total_chunks": think_chunk_index,
            "thinking_text": "".join(think_parts) if think_parts else None,
            "node": "node2",
        })

    # result 一定有值（要么流式要么降级非流式）
    prompts = result.get("generate_prompts", [])
    raw_text = result.get("raw_text", "")

    # 用全新 dict 返回，避免 in-place 修改导致 merge 丢失
    new_node2 = dict(node2)
    new_node2["generate_prompts"] = prompts               # JSON 数组，Node 3 直接用
    new_node2["planning_result"] = raw_text               # 原始文本，前端展示用
    new_node2["ratio"] = ratio
    new_node2["count"] = count
    # 🔁 缓存模特图压缩结果，供 Node3 复用（避免重复 PIL 压缩）
    new_node2["compressed_model_images"] = model_images

    output = {"phase": "node2_planning", "node2": new_node2}
    print(f"[node2] _plan 输出 node2 keys={list(new_node2.keys())}, "
          f"generate_prompts={len(prompts)} 屏, "
          f"总耗时={time.time() - t0:.2f}s", flush=True)
    return output
