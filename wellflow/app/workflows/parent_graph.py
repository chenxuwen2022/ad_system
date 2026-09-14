"""父图：Node1 → C1 → Node2 → C2 → Node3 → C3_review → finalize。

重做/确认机制：
  - C3 interrupt 让用户查看生图结果，可选择「重做」或「确认」
  - 重做 → 所有 work_items status 重置为 pending → 回到 Node 3 重新生图
  - 确认 → finalize 归档，phase=done

带 AsyncPostgresSaver checkpoint 和 HITL interrupt。
"""

from __future__ import annotations

from typing import Any


PARENT_RECURSION_LIMIT = 50  # 支持多次重做循环（每次循环消耗 ~4-5 步）


def build_graph(checkpointer=None):
    """构建父图。langgraph 未安装时抛 ImportError。"""
    try:
        from langgraph.graph import StateGraph, START, END
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "langgraph 未安装。请 pip install langgraph langgraph-checkpoint-postgres"
        ) from exc

    from wellflow.app.workflows.state import TaskState
    from wellflow.app.workflows import node1_graph, node2_graph, node3_graph
    from wellflow.app.workflows.finalize import finalize_task

    parent = StateGraph(TaskState)

    n1 = node1_graph.build_graph()
    n2 = node2_graph.build_graph()
    n3 = node3_graph.build_graph()

    parent.add_node("node1_research", n1)
    parent.add_node("c1_confirm", _c1_interrupt_node)
    parent.add_node("node2_planning", n2)
    parent.add_node("c2_confirm", _c2_interrupt_node)
    parent.add_node("node3_delivery", n3)
    parent.add_node("c3_review", _c3_interrupt_node)
    parent.add_node("finalize", finalize_task)

    parent.add_edge(START, "node1_research")
    parent.add_edge("node1_research", "c1_confirm")
    parent.add_edge("c1_confirm", "node2_planning")
    parent.add_edge("node2_planning", "c2_confirm")
    parent.add_edge("c2_confirm", "node3_delivery")
    parent.add_edge("node3_delivery", "c3_review")

    # C3 interrupt resume 后的条件路由：redo → 重回 Node 3 生图；confirm → finalize
    parent.add_conditional_edges(
        "c3_review",
        _route_after_c3,
        {
            "redo": "node3_delivery",
            "confirm": "finalize",
        },
    )
    parent.add_edge("finalize", END)

    return parent.compile(checkpointer=checkpointer)


def _route_after_c3(state: dict[str, Any]) -> str:
    """C3 interrupt resume 后的路由函数。

    user_action 由 _c3_interrupt_node resume 时写入 state['_c3_action']。
    """
    action = state.get("_c3_action", "confirm")
    print(f"[c3_review] 路由决策: action={action}", flush=True)
    return action


# ---------------------------------------------------------------------------
# HITL interrupt 节点：用 langgraph.types.interrupt() 暂停 graph
# ---------------------------------------------------------------------------


def _c1_interrupt_node(state: dict[str, Any]) -> dict[str, Any]:
    """Node 1 后暂停：等用户确认/修改 VLM 生成的商品识别报告 + 上传模特图 + 选择画面比例。"""
    from langgraph.types import interrupt

    node1 = state.get("node1", {})
    node2 = state.get("node2", {})
    # product_insight 现在就是 VLM 直接输出的文本报告（字符串）
    product_insight = node1.get("product_insight", "")

    # request.product_images 现在是路径列表，前端需要 data URI 展示
    # 🔑 优先复用 Node1 已缓存的压缩结果，跳过二次 PIL 压缩
    req_for_frontend = dict(state.get("request", {}))
    cached = node1.get("compressed_images") or []
    if cached:
        print(f"[c1] 🔁 复用 Node1 已缓存的 {len(cached)} 张商品图 data URI（跳过二次压缩）", flush=True)
        req_for_frontend["product_images"] = cached
    else:
        from wellflow.app.utils.image_store import paths_to_data_uris
        req_for_frontend["product_images"] = paths_to_data_uris(
            req_for_frontend.get("product_images") or []
        )

    interrupt_value = {
        "node": "c1",
        "hint": "请确认或修改商品识别报告，并可上传模特图、选择画面比例。",
        "product_insight": product_insight,
        "request": req_for_frontend,  # product_images 已转 data URI，state 里仍是路径
        "schema": {
            "confirmed_report": "str",
            "model_images": "list[base64_data_uri] (可选)",
            "ratio": "str (如 9:16竖版, 默认 9:16竖版)",
        },
    }
    # interrupt() 暂停 graph；resume 时返回前端传来的 values
    user_values = interrupt(interrupt_value)
    confirmed = user_values.get("confirmed_report") if isinstance(user_values, dict) else None
    model_images = user_values.get("model_images") if isinstance(user_values, dict) else None
    ratio = user_values.get("ratio") if isinstance(user_values, dict) else None
    count = user_values.get("count") if isinstance(user_values, dict) else None

    state["phase"] = "c1_resumed"
    state["interrupt"] = None
    # 如果前端传了修改后的报告，覆盖 product_insight
    if confirmed is not None and confirmed != product_insight:
        state["node1"] = {**node1, "product_insight": confirmed}
    # 把用户上传的模特图 + ratio + count 存到 node2 state
    if model_images:
        node2["model_images"] = model_images
    if ratio:
        node2["ratio"] = ratio
    # count 默认值从 config 读，前端没传时用 node2_prompt_count_default（3）
    from wellflow.app.config import settings as _settings
    default_count = _settings.node2_prompt_count_default
    try:
        node2["count"] = max(1, min(10, int(count))) if count else default_count
    except (ValueError, TypeError):
        node2["count"] = default_count
    state["node2"] = node2
    return state


def _c2_interrupt_node(state: dict[str, Any]) -> dict[str, Any]:
    """Node 2 后暂停：用户可查看 VLM 生成的 N 屏 prompt，选择每个 prompt 生成几张图后继续。"""
    from langgraph.types import interrupt

    node2 = state.get("node2", {})
    count = node2.get("count", 3)
    prompts = node2.get("generate_prompts") or []
    # 默认每个 prompt 生成 1 张，总输出 count × images_per_prompt
    from wellflow.app.config import settings as _settings
    default_images_per_prompt = _settings.node3_images_per_prompt_default

    print(f"[c2] _c2_interrupt_node: generate_prompts={len(prompts)} 屏, count={count}", flush=True)
    interrupt_value = {
        "node": "c2",
        "hint": f"VLM 已生成 {len(prompts)} 屏提示词（共请求 {count} 屏）。"
                f"为每个提示词选择生成几张图（默认 {default_images_per_prompt}），"
                f"最终将生成 {len(prompts)} × N = 总共几张图。",
        "generate_prompts": prompts,
        "planning_result": node2.get("planning_result", ""),  # 原始 JSON 文本，debug 用
        "default_images_per_prompt": default_images_per_prompt,
        "schema": {
            "images_per_prompt": "int (每个提示词生成几张图，默认 1，范围 1-5)",
            "image_model": "str (可选，更换生图模型)",
            "ratio": "str (可选，更换画面比例)",
        },
    }
    print(f"[c2] interrupt_value planning_result len={len(interrupt_value['planning_result'])}", flush=True)
    user_values = interrupt(interrupt_value)
    image_model = None
    ratio_val = None
    images_per_prompt = None
    selected_prompt_indices = None
    if isinstance(user_values, dict):
        image_model = user_values.get("image_model")
        ratio_val = user_values.get("ratio")
        images_per_prompt = user_values.get("images_per_prompt")
        selected_prompt_indices = user_values.get("selected_prompt_indices")

    state["phase"] = "c2_resumed"
    state["interrupt"] = None
    if image_model:
        node2["image_model"] = image_model
    if ratio_val:
        node2["ratio"] = ratio_val
    # 写入 images_per_prompt（限制 1-5）
    try:
        node2["images_per_prompt"] = max(1, min(5, int(images_per_prompt))) if images_per_prompt else default_images_per_prompt
    except (ValueError, TypeError):
        node2["images_per_prompt"] = default_images_per_prompt

    # 🔑 根据 selected_prompt_indices 过滤 generate_prompts
    original_count = len(prompts)
    if selected_prompt_indices is not None and len(selected_prompt_indices) > 0:
        # 过滤 + 按索引顺序取 prompt
        filtered = [prompts[i] for i in selected_prompt_indices if 0 <= i < len(prompts)]
        if filtered:
            node2["generate_prompts"] = filtered
            prompts = filtered
            print(f"[c2] 🔑 用户选择了 {len(prompts)}/{original_count} 个 prompts "
                  f"(indices={selected_prompt_indices})", flush=True)
        else:
            print(f"[c2] ⚠️ selected_prompt_indices={selected_prompt_indices} 全部越界，使用全量 {original_count}", flush=True)
    else:
        print(f"[c2] 用户未选择，使用全部 {original_count} 个 prompts", flush=True)

    total = len(prompts) * node2["images_per_prompt"]
    print(f"[c2] ✅ resume: images_per_prompt={node2['images_per_prompt']}, "
          f"预计总生图数 = {len(prompts)} × {node2['images_per_prompt']} = {total}", flush=True)
    state["node2"] = node2
    return state


def _c3_interrupt_node(state: dict[str, Any]) -> dict[str, Any]:
    """Node 3 生图后暂停：用户查看多屏生成图片，可「重做」或「确认」。

    三源输入回顾：商品图（Node 1 上传）+ 模特图（C1 上传）+ prompt（Node 2 生成）
    """
    from langgraph.types import interrupt

    node3 = state.get("node3", {})
    outputs = node3.get("outputs", [])

    # 🔑 优先用 Node3 一次性组装好的缓存，跳过 PIL
    ref_data_uris: list[str] = node3.get("reference_images_data_uris") or []

    # 兜底：老任务没有缓存时才现场压
    if not ref_data_uris:
        ref_paths: list[str] = node3.get("reference_images", [])
        from wellflow.app.utils.image_store import paths_to_data_uris
        ref_data_uris = paths_to_data_uris(ref_paths)
        print(f"[c3_review] ⚠️ 无缓存，现场压缩 {len(ref_paths)} 张参考图", flush=True)

    print(f"[c3_review] _c3_interrupt_node outputs={len(outputs)}, "
          f"reference_images data_uris={len(ref_data_uris)}", flush=True)

    failed_items = node3.get("failed_items", [])
    prompts = state.get("node2", {}).get("generate_prompts") or []

    # 🔑 按 prompt_index 分组，前端可以直接分 tab / 分组展示
    # grouped_outputs[i] = {"prompt_index": i, "prompt": prompts[i], "images": [...], "errors": [...]}
    grouped_outputs: list[dict[str, Any]] = []
    for pi, prompt in enumerate(prompts):
        group_images = sorted(
            [o for o in outputs if o.get("prompt_index", 0) == pi],
            key=lambda o: o.get("variant_index", 0),
        )
        group_errors = sorted(
            [f for f in failed_items if f.get("prompt_index", 0) == pi],
            key=lambda f: f.get("variant_index", 0),
        )
        grouped_outputs.append({
            "prompt_index": pi,
            "prompt": prompt,
            "images": group_images,
            "errors": group_errors,  # 该 prompt 下失败的 work_items（含 error 字段）
        })

    # 构建 hint —— 真实反映成功/失败情况
    n_success = len(outputs)
    n_failed = len(failed_items)
    n_total = n_success + n_failed
    if n_failed == 0:
        hint = f"已生成 {n_success} 张图片（{len(grouped_outputs)} 个提示词）。可选择「重做」重新生成，或「确认」结束任务。"
    elif n_success == 0:
        hint = f"全部生图失败（{n_failed}/{n_total}）。可选择「重做」重新生成。"
    else:
        hint = f"部分生图成功（{n_success}/{n_total}），失败 {n_failed} 张。可选择「重做」重新生成，或「确认」结束任务。"

    interrupt_value = {
        "node": "c3",
        "hint": hint,
        "outputs": outputs,              # 扁平列表，兼容旧前端
        "failed_items": failed_items,    # 失败详情列表（含 work_item_id + error）
        "grouped_outputs": grouped_outputs,  # 按 prompt 分组，推荐前端用这个
        "reference_images": ref_data_uris,  # data URI 列表，前端展示参考图
        "generate_prompts": prompts,        # Node 2 的 prompt 数组（与 grouped_outputs 一一对应）
        "planning_result": state.get("node2", {}).get("planning_result", ""),
        "stats": {                        # 成功/失败计数，前端方便展示
            "success": n_success,
            "failed": n_failed,
            "total": n_total,
        },
        "schema": {
            "action": "'redo' | 'confirm'",  # 重做 或 确认
        },
    }

    # interrupt() 暂停 graph；resume 时返回前端传来的 values
    user_values = interrupt(interrupt_value)
    action = "confirm"  # 默认确认
    image_model = None
    ratio_val = None
    if isinstance(user_values, dict):
        action = user_values.get("action", "confirm")
        image_model = user_values.get("image_model")
        ratio_val = user_values.get("ratio")

    print(f"[c3_review] resume action={action}, image_model={image_model or '(不变)'}, ratio={ratio_val or '(不变)'}", flush=True)

    # 写入临时字段供 _route_after_c3 条件边使用
    state["_c3_action"] = action

    if action == "redo":
        # 重做：把所有 work_items status 重置为 pending，清空 outputs
        work_items = node3.get("work_items", [])
        for item in work_items:
            item["status"] = "pending"
            item["retry_count"] = item.get("retry_count", 0) + 1
            item["error"] = ""
        node3["work_items"] = work_items
        node3["outputs"] = []  # 清空，让 _run_gen 重新填充

        # 如果用户换了生图模型 / ratio，更新 node2（Node 3 从这里读）
        node2 = state.get("node2", {})
        if image_model:
            node2["image_model"] = image_model
            print(f"[c3_review] ↺ 更换生图模型: {image_model}", flush=True)
        if ratio_val:
            node2["ratio"] = ratio_val
            print(f"[c3_review] ↺ 更换画面比例: {ratio_val}", flush=True)
        state["node2"] = node2

        state["node3"] = node3
        state["phase"] = "c3_retrying"
        state["interrupt"] = None
        print(f"[c3_review] ↺ 重做: 重置 {len(work_items)} 个 work_items 为 pending", flush=True)
    else:
        # 确认：继续到 finalize
        state["phase"] = "c3_resumed"
        state["interrupt"] = None
        print(f"[c3_review] ✅ 确认: 继续到 finalize", flush=True)

    return state
