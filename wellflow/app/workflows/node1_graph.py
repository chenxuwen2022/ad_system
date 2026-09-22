"""Node 1 子图：input_analyzer → VLM 识别 → END。

流式/非流式策略（根据 reasoning_effort 预先选择，不做运行时降级）：
  - effort == "low"        → 非流式 analyze_product
  - effort == "close" / "medium" / "high" → 流式 stream_analyze_product
流式路径下每个 delta token 立即 publish 到 SSE event_bus，前端实时逐字输出。

💭 Thinking 策略（2026-09-21 对齐 node2/node3）：
  直接把模型原始 reasoning_content 按 token 流推给 thinking_chunk SSE 事件，
  不做任何格式约束或派生。这样 thinking 面板会**先于**报告正文出现，
  真正反映"模型在思考时用户在看什么"。
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

    💭 thinking：reasoning_content 直接按 token 推 thinking_chunk SSE，
    和 content 通道（report_chunk）并行独立，前端先看"思考中"再看报告。

    🔴 锁定守卫：若 state.node1.report_locked=True，说明报告已被 C1 确认并锁定，
    当前任务内不得再重新生成 —— 直接跳过，返回原 node1 状态不变。
    """
    from wellflow.app.nodes import product_analyzer
    from wellflow.app.event_bus import publish

    task_id = state.get("task_id", "")

    # —— 锁定守卫：已锁定则绝不能重跑 Node1 ——
    if bool((state.get("node1") or {}).get("report_locked")):
        print(f"[node1] 🛡️ task={task_id} node1 报告已锁定，拒绝重新生成", flush=True)
        if task_id:
            publish(task_id, "message", {
                "text": (
                    "产品报告已确认并锁定，本任务内无法再修改或重新生成。"
                    "如需调整商品信息，请新建任务重新生成报告。"
                ),
            })
        # 返回空更新 + 保持停在 C1（让 graph 走 interrupt 路径到 C1，
        # 但 C1 自己会识别已锁定并放行到 Node2；这里为了保守只返回 phase）
        return {"phase": "c1_confirm"}

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

    # --- Step 4: 统一流式 + reasoning_effort（从 settings 读取，Node1/Node2/Node3 各自可配） ---
    from wellflow.app.config import settings as _settings
    effort = _settings.node1_reasoning_effort

    t0 = time.time()
    full_report_parts: list[str] = []
    content_chunk_index = 0
    think_parts: list[str] = []  # 💭 原始 reasoning_content 累积
    think_chunk_index = 0
    first_content_ts = None
    first_think_ts = None

    print(f"[node1] 📌 reasoning_effort={effort} → 流式 stream_analyze_product", flush=True)

    # ── 流式消费 ──────────────────────────────────────────────────────
    # reasoning_content → 直接推 thinking_chunk SSE（和 node2/node3 完全一致）
    # content          → 推 report_chunk SSE
    async for item in product_analyzer.stream_analyze_product(
        images=images,
        user_text=user_text,
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
            # 💭 原始 reasoning_content → 直接推，不做任何格式转化
            if first_think_ts is None:
                first_think_ts = time.time()
                print(f"[node1] 💭 首 thinking token 到达 TTFB={first_think_ts - t0:.2f}s", flush=True)
            think_parts.append(text)
            think_chunk_index += 1
            publish(task_id, "thinking_chunk", {
                "chunk": text,
                "index": think_chunk_index,
                "node": "node1",
            })
        else:
            # ── content 通道 ─────────────────────────────────────────
            if first_content_ts is None:
                first_content_ts = time.time()
                print(f"[node1] 🟢 首 content token TTFB={first_content_ts - t0:.2f}s"
                      + (f" (thinking 耗时={first_content_ts - first_think_ts:.2f}s)" if first_think_ts else "")
                      , flush=True)

            # ── 正常 content chunk：追加 + 推 report_chunk ────────
            full_report_parts.append(text)
            content_chunk_index += 1
            publish(task_id, "report_chunk", {"chunk": text, "index": content_chunk_index, "node": "node1"})

    # ── 后处理 ──────────────────────────────────────────────────────────
    full_report = "".join(full_report_parts)
    full_thinking = "".join(think_parts)
    report_body, next_actions = _split_next_actions(full_report)

    # 归一化为稳定 key 的四块结构（前端"重点洞察"面板只认这个，不认 prompt 字段名）
    from wellflow.app.prompt.report_sections import build_report_sections
    report_sections = build_report_sections(report_body)
    total_ts = time.time()
    print(f"[node1] ✅ VLM 完成: 报告 {len(report_body)} 字, "
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
        "thinking_text": full_thinking or None,
        "node": "node1",
    })

    # —— next_actions（LLM 动态引导语）不再作为独立 SSE message 事件推送，
    #    而是存入 node1 state，由 parent_graph 在 C1 interrupt 时作为 hint 下发给前端。
    #    这样避免了独立 message 事件被 append 到报告正文再被 interrupt 的 replace 覆盖的问题。

    return {
        "phase": "node1_vlm_done",
        "node1": {
            **state.get("node1", {}),
            "input_analysis": analysis,
            "product_insight": report_body,
            "report_sections": report_sections,
            # 💬 LLM 动态生成的下一步引导语（报告正文 ---NEXT--- 分隔线之下的部分）
            #    存入 state 后由 parent_graph 在 C1 interrupt 时作为 hint 下发
            "next_actions": next_actions,
            # 🔁 缓存已压缩的商品图 data URIs，供 Node2 复用（避免重复 PIL 压缩 ~1.2s）
            "compressed_images": images,
            # 💭 持久化 thinking 原文（未格式化），刷新后前端可恢复展示
            "thinking_text": full_thinking or "",
        },
    }


def _split_next_actions(raw: str) -> tuple[str, str]:
    """按 ---NEXT--- 把 LLM 返回拆成 (报告正文, 引导语)。

    - 找到分隔符：[0] 存 product_insight，[1] 去掉前后空行后作为 next_actions 返回
    - 找不到：完整 raw 当报告正文，引导语返回空串（调用方走 hardcoded 兜底）
    """
    if not raw:
        return "", ""
    marker = "---NEXT---"
    idx = raw.rfind(marker)
    if idx == -1:
        return raw.strip(), ""
    body = raw[:idx].rstrip()
    actions = raw[idx + len(marker):].strip()
    return body, actions
