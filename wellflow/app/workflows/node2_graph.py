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
    """VLM 流式产出 N 套商拍方案，逐 token 推送 SSE；方案内 JSON 闭合时立刻推送单套 done。

    复用 Node1 / Node3 的流式模式：thinking_chunk / scheme_chunk，
    与 Node3 对齐——Node3 每个 variant 完整生成就推 prompt_chunk_done，
    Node2 现在也改为每个方案 JSON 完整闭合就推 scheme_chunk_done，
    前端一边生成一边看到方案卡片，不用等全部跑完。

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

    from wellflow.app.config import settings as _settings
    scheme_count: int = int(req.get("scheme_count") or _settings.node2_scheme_count_default)

    if task_id:
        publish(task_id, "phase", {"phase": "node2_plan_scheme", "scheme_count": scheme_count})

    cached_product = node1.get("compressed_images") or []
    product_image_paths: list[str] = req.get("product_images") or []
    product_images = cached_product
    if not product_images and product_image_paths:
        from wellflow.app.utils.image_store import paths_to_data_uris
        product_images = await asyncio.to_thread(paths_to_data_uris, product_image_paths)

    print(f"[node2] _plan_schemes 输入: scheme_count={scheme_count}, "
          f"user_requirement={'有' if user_requirement else '无'}, "
          f"product_images={len(product_images)}", flush=True)

    # reasoning_effort 从 settings.node2_reasoning_effort 读取（默认 low，可配）
    from wellflow.app.config import settings as _settings
    effort = _settings.node2_reasoning_effort

    t0 = time.time()
    content_parts: list[str] = []
    think_parts: list[str] = []
    content_chunk_index = 0
    think_chunk_index = 0
    first_content_ts = None
    first_think_ts = None

    # --- 逐 scheme 流式 JSON 探测器 ---
    # JSON 流大致长这样：{"schemes": [ {s0完整}, {s1完整}, ... ]}
    # 我们维护顶层数组里当前正在累积的那个 scheme 的完整文本，
    # 一旦它闭合（花括号 depth 回到 0），立刻尝试解析并推送 done。
    _scheme_buffer = ""          # 当前 scheme 的 JSON 文本累积
    _brace_depth = 0             # 在 schemes[i] 内部时的花括号深度
    _array_depth = 0             # 数组嵌套深度（主 schemes 数组 = 1）
    _in_string = False           # JSON 字符串内（防止把字符串里的 {}[] 当结构）
    _escape_next = False         # 字符串内 \ 转义下一个字符
    _per_scheme_index = 0        # 下一个闭合的 scheme 的 0-based 序号
    _extracted_schemes: list[dict[str, Any]] = []  # 已经逐个成功解析的方案

    def _feed_scheme_json(chunk: str) -> None:
        """把一段 JSON 文本喂给方案探测器，闭合一个就 publish 一次 done。

        关键点：
          - 顶层 JSON 里的 "_meta" 之类字段的字符串里如果含 '}'，
            我们在顶层（_brace_depth == 0）完全跳过它们，不污染 depth。
          - 但一旦进入 scheme 对象（_brace_depth > 0），字符串里的 '{' '}'
            要原样追加到 _scheme_buffer，不能影响 depth。
        """
        nonlocal _scheme_buffer, _brace_depth, _array_depth
        nonlocal _in_string, _escape_next, _per_scheme_index, _extracted_schemes
        for ch in chunk:
            # scheme 内部字符串：原样写入 buffer，不影响结构 depth
            if _in_string and _brace_depth > 0:
                _scheme_buffer += ch
                if _escape_next:
                    _escape_next = False
                    continue
                if ch == "\\":
                    _escape_next = True
                    continue
                if ch == '"':
                    _in_string = False
                continue

            # 顶层字符串（_meta 等）：完全跳过，不进入 scheme_buffer
            if _in_string and _brace_depth == 0:
                if _escape_next:
                    _escape_next = False
                    continue
                if ch == "\\":
                    _escape_next = True
                    continue
                if ch == '"':
                    _in_string = False
                continue

            # 字符串外：遇到引号
            if ch == '"':
                _in_string = True
                _escape_next = False
                if _brace_depth > 0:
                    _scheme_buffer += ch
                continue

            # 还没进入顶层数组 / scheme —— 跳过，只对 '[' 感兴趣
            if _array_depth < 1 and _brace_depth == 0:
                if ch == "[":
                    _array_depth = 1
                continue

            # --- 已进入顶层 schemes 数组 / scheme 对象内部 ---
            if ch == "{":
                if _brace_depth == 0:
                    # 数组里第一次遇到 { —— scheme 对象起点
                    _scheme_buffer = "{"
                    _brace_depth = 1
                else:
                    # 已在 scheme 内部（嵌套对象）：追加 + 深度++
                    _scheme_buffer += ch
                    _brace_depth += 1
            elif ch == "}":
                if _brace_depth > 0:
                    _scheme_buffer += ch
                    _brace_depth -= 1
                    if _brace_depth == 0:
                        # 当前 scheme 完整闭合 → 尝试解析
                        _try_extract_and_publish(_scheme_buffer)
                        _scheme_buffer = ""
            elif ch == "[":
                if _brace_depth > 0:
                    # scheme 内部的嵌套数组（如 visual_keywords）
                    _scheme_buffer += ch
            elif ch == "]":
                if _brace_depth > 0:
                    # scheme 内部数组结束
                    _scheme_buffer += ch
                else:
                    # schemes 主数组结束
                    _array_depth = 0
                    if _scheme_buffer.strip():
                        _try_extract_and_publish(_scheme_buffer)
                        _scheme_buffer = ""
            elif _brace_depth > 0:
                # 当前 scheme 内部的任何其他字符
                _scheme_buffer += ch

    def _try_extract_and_publish(scheme_json: str) -> None:
        """尝试解析一个闭合后的 scheme JSON，成功就 publish scheme_chunk_done。"""
        nonlocal _per_scheme_index
        import json as _json_mod
        parsed_scheme: dict[str, Any] | None = None
        try:
            obj = _json_mod.loads(scheme_json)
            if isinstance(obj, dict):
                parsed_scheme = obj
        except Exception:
            # 兜底：复用 planning_scheme 里的 _extract_json 容错链
            try:
                # _extract_json 入口是从整段 JSON 里抽 scheme；这里已经是 scheme 片段，
                # 直接尝试 json.loads 最靠谱，失败再跳过（让最终汇总时兜底）。
                from wellflow.app.nodes.planning_scheme import _extract_json as _ep
                obj2 = _ep(scheme_json)
                if isinstance(obj2, dict):
                    parsed_scheme = obj2
            except Exception:
                parsed_scheme = None

        if parsed_scheme is None:
            print(f"[node2] ⚠️ per-scheme JSON 解析失败，跳过实时推送 (idx={_per_scheme_index})", flush=True)
            _per_scheme_index += 1
            return

        # 重归一化 scheme_index：按到达顺序给 0..N-1，覆盖 VLM 可能错乱的 index
        parsed_scheme["scheme_index"] = _per_scheme_index
        _extracted_schemes.append(parsed_scheme)

        if task_id:
            publish(task_id, "scheme_chunk_done", {
                "scheme_index": _per_scheme_index,
                "scheme_name": parsed_scheme.get("scheme_name") or f"方案{_per_scheme_index + 1}",
                "scheme": parsed_scheme,   # 前端立刻展示
                "per_scheme_index": _per_scheme_index,
                "node": "node2",
            })
            print(f"[node2] ✅ 实时方案 #{_per_scheme_index} 已闭合并推送 done", flush=True)

        _per_scheme_index += 1

    # ---- 开始流式 VLM ----
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
            # 实时解析方案边界
            _feed_scheme_json(text)

    raw_text = "".join(content_parts)
    full_thinking = "".join(think_parts)

    # ---- 最终汇总解析 —— 作为 per-scheme 提取的兜底 & 数量/索引统一 ----
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

    # scheme_index 统一重归一化（防御 VLM 返回错乱的 index 值）
    for _i, _s in enumerate(schemes):
        if isinstance(_s, dict):
            _s["scheme_index"] = _i

    # 防御交叉检查：detector 实时提取 vs 最终完整 JSON 解析 —— 两者数量应一致
    if len(_extracted_schemes) and len(_extracted_schemes) != len(schemes):
        print(
            f"[node2] ⚠️ detector 实时提取 {len(_extracted_schemes)} 套 ≠ "
            f"最终解析 {len(schemes)} 套。实时推送可能有遗漏/重复。",
            flush=True,
        )

    total_ts = time.time()
    print(f"[node2] ✅ 流式 VLM 完成: schemes={len(schemes)} 套, "
          f"raw={len(raw_text)} 字, thinking={len(full_thinking)} 字, "
          f"content_chunks={content_chunk_index}, think_chunks={think_chunk_index}, "
          f"per_scheme_dones={len(_extracted_schemes)}, "
          f"总耗时={total_ts - t0:.2f}s"
          + (f", 首think={first_think_ts - t0:.2f}s" if first_think_ts else "")
          + (f", 首content={first_content_ts - t0:.2f}s" if first_content_ts else "")
          , flush=True)

    # 最后推一个汇总 done（和旧版同 shape，前端 C2 阶段如果需要完整列表可以直接用）
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
        "base_scheme_count": len(schemes),  # 🛑 锚点：Node2 正向产出的原始方案数
    }

    output = {"phase": "node2_plan_scheme", "node2": new_node2}
    print(f"[node2] _plan_schemes 输出: schemes={len(schemes)} 套, base_scheme_count={len(schemes)}", flush=True)
    return output
