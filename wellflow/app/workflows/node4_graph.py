"""Node 4 子图：Node 3 的 generate_prompts 数组 → 调 LLM /images/generations → 归档 outputs。

两源输入：
  - node1.compressed_images: Node 1 已压缩好的商品图 data URI（**优先复用，避免重复 PIL**）
  - node3.reference_images: 三类参考图 {"mannequin": [...], "scene": [...], "outfit": [...]}（文件路径，Node4 现场转 data URI）
  - node3.generate_prompts:  Node 3 prompt_generation 产出的 prompt 数组
  - node3.per_prompt_size:   C3 interrupt 用户选的每套 prompt 的图片规格（如 ["3:4", "3:4"]）

完整流程（2 节点，子图内部 2 边）：
  1. _prepare:  组装 work_items（一 prompt 一 item，N 强制 =1）+ 统一转一次所有参考图 data URI
  2. _run_gen:  并发逐 work_item 独立调 LLM /images/generations → 写 outputs/failed_items

⚠️ 新语义：per_prompt_count 已彻底删除。Node4 不再有批量 n>1 概念，
  每张图 = 一次独立 API 调用。redo/confirm 机制在 parent_graph 的 c4_review 实现。

⚠️ 模型策略：
  Node4 生图动态从 new-api 拉 image 模型列表（qwen-image-3.0 优先），
  不从前端 interrupt、chat.py 分类器、state.node3.image_model 里取模型。
  底层「模型拉取 + 单次生图 + 降级链」已抽至 wellflow.app.llm.image_gen_service，
  API 端点（mannequin /generate）也共用同一份逻辑，确保行为一致。
"""

from __future__ import annotations

from typing import Any

from wellflow.app.llm.image_gen_service import get_image_models, generate_single_image


# ---------------------------------------------------------------------------
# ratio → pixel size helper（Node4 特有语义，留在本文件）
# ---------------------------------------------------------------------------


def _ratio_to_size(ratio: str) -> str:
    """ratio 字符串 → GPT Image /images/edits 支持的固定像素 size。"""
    from wellflow.app.config import settings
    clean = ratio.replace("竖版", "").replace("横版", "").replace("方形", "").strip()
    return settings.image_ratio_to_pixel_size_gpt.get(clean, "1024x1792")


# ---------------------------------------------------------------------------
# LangGraph 构建
# ---------------------------------------------------------------------------


def build_graph():
    try:
        from langgraph.graph import StateGraph, START, END
    except ImportError as exc:
        raise ImportError(
            "langgraph 未安装。请 pip install langgraph langgraph-checkpoint-postgres"
        ) from exc

    from wellflow.app.workflows.state import TaskState

    graph = StateGraph(TaskState)

    graph.add_node("prepare_work_items", _prepare)
    graph.add_node("run_generation", _run_gen)

    graph.add_edge(START, "prepare_work_items")
    graph.add_edge("prepare_work_items", "run_generation")
    graph.add_edge("run_generation", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# 节点 1：准备 work_items
# ---------------------------------------------------------------------------


async def _prepare(state: dict[str, Any]) -> dict[str, Any]:
    """组装 work_items（一 prompt 一 item，N 强制 =1）。

    🔑 data URI 策略（与 Node2/Node3 一致）：
      - 商品图：优先复用 node1.compressed_images（Node1 已压缩好的 data URI 缓存），
        只有在缓存缺失时才用 request.product_images 现场转。
      - 三类参考图：node3.reference_images 是用户上传的文件路径，Node4 首次用到，现场统一转一次。
    """
    import asyncio
    import time as _time

    from wellflow.app.event_bus import publish as _eb
    from wellflow.app.utils.image_store import paths_to_data_uris

    task_id = state.get("task_id", "")
    if task_id:
        _eb(task_id, "phase", {"phase": "node4_prepare"})

    node1 = state.get("node1", {}) or {}
    node3 = state.get("node3", {}) or {}
    node4_in = state.get("node4", {}) or {}
    req = state.get("request", {})

    # ---- 商品图：优先吃 Node1 缓存，避免重复 PIL 压缩 ----
    product_uris: list[str] = node1.get("compressed_images") or []
    if not product_uris:
        _t0 = _time.time()
        product_paths: list[str] = req.get("product_images") or []
        if product_paths:
            product_uris = await asyncio.to_thread(paths_to_data_uris, product_paths)
            print(f"[node4] prepare: 商品图缓存缺失，现场转 data URI {len(product_paths)} 张，耗时 {_time.time() - _t0:.2f}s", flush=True)

    # ---- 三类参考图：首次用到，现场统一转一次 data URI ----
    ref_images: dict[str, list[str]] = node3.get("reference_images") or {}
    mannequin_paths = ref_images.get("mannequin") or []
    scene_paths = ref_images.get("scene") or []
    outfit_paths = ref_images.get("outfit") or []

    _t1 = _time.time()
    mannequin_uris = await asyncio.to_thread(paths_to_data_uris, mannequin_paths) if mannequin_paths else []
    scene_uris = await asyncio.to_thread(paths_to_data_uris, scene_paths) if scene_paths else []
    outfit_uris = await asyncio.to_thread(paths_to_data_uris, outfit_paths) if outfit_paths else []

    all_ref_uris = [*product_uris, *mannequin_uris, *scene_uris, *outfit_uris]

    _t2 = _time.time()
    print(f"[node4]  prepare 开始 task={task_id[:8]}: "
          f"商品图 {len(product_uris)} 张(缓存={bool(product_uris)}), "
          f"mannequin {len(mannequin_paths)}, scene {len(scene_paths)}, outfit {len(outfit_paths)}, "
          f"data URI 转换耗时 {_t2 - _t1:.2f}s", flush=True)

    # ---- 读 Node 3 的 generate_prompts ----
    prompts: list[str] = node3.get("generate_prompts") or []

    per_prompt_size: list[str] = node3.get("per_prompt_size") or []

    # 始终构造新 dict 返回，不原地 mutate state 里的 node4
    node4: dict[str, Any] = dict(node4_in)
    node4["reference_images_data_uris"] = all_ref_uris

    if not prompts:
        print("[node4] ⚠️ generate_prompts 为空，无法生成 work_items", flush=True)
        node4["work_items"] = []
        node4["reference_images"] = all_ref_uris
        return {"phase": "node4_prepare", "node4": node4}

    # ---- 组装 work_items —— 一 prompt 一 work_item ----
    work_items: list[dict[str, Any]] = []
    shot_names: list[str] = []
    for pi, prompt in enumerate(prompts):
        ratio_str = per_prompt_size[pi] if pi < len(per_prompt_size) else "3:4"
        size = _ratio_to_size(ratio_str)
        shot_id = f"shot-{pi+1:02d}"
        shot_names.append(shot_id)
        work_items.append({
            "work_item_id": shot_id,
            "prompt_index": pi,
            "prompt": prompt,
            "ratio": ratio_str,
            "size": size,
            "status": "pending",
        })

    node4["work_items"] = work_items
    node4["reference_images"] = all_ref_uris  # data URI 列表，供 run_gen 备用
    print(f"[node4] 📥 入队 {len(work_items)} 张（按 prompt_index 顺序）: "
          f"{', '.join(shot_names)}", flush=True)
    return {"phase": "node4_prepare", "node4": node4}


# ---------------------------------------------------------------------------
# 节点 2：调 LLM 生图（含原来 archive 的输出落盘逻辑）
# ---------------------------------------------------------------------------


async def _run_gen(state: dict[str, Any]) -> dict[str, Any]:
    """并发调 LLM 生图 —— 每个 work_item 独立一次 API 调用（n 强制 =1）。

    模型选择：动态从 new-api 拉 image 模型列表（qwen-image-3.0 优先），
    不从 state.node3.image_model 读取。
    """
    import asyncio
    import time

    from wellflow.app.event_bus import publish as _eb

    task_id = state.get("task_id", "")
    if task_id:
        _eb(task_id, "phase", {"phase": "node4_generation"})

    node4_in = state.get("node4", {}) or {}
    work_items = list(node4_in.get("work_items", []) or [])  # 拷贝 list，in-place 状态更新就不会碰 state
    cached_ref_uris: list[str] = node4_in.get("reference_images_data_uris") or []

    refs = cached_ref_uris  # _prepare 已经保证有 URI；没有就空列表

    pending_items = [it for it in work_items if it.get("status") in ("pending", "redo")]
    outputs: list[dict[str, Any]] = []
    from wellflow.app.config import settings
    WORKERS = settings.node4_gen_concurrency

    # 模型链一次拉取（供日志用），generate_single_image 内部也会自己拉一份
    try:
        _plan_models = await get_image_models()
        _plan_first = _plan_models[0] if _plan_models else "(未指定)"
    except Exception:
        _plan_first = "(未指定)"

    print(f"[node4] 🏃 run_gen 启动 task={task_id[:8]}: "
          f"入队 {len(pending_items)} 张, worker={WORKERS}, "
          f"计划模型={_plan_first}, 参考图 refs={len(refs)}", flush=True)

    queue: asyncio.Queue[dict] = asyncio.Queue()
    for it in pending_items:
        queue.put_nowait(it)
    result_q: asyncio.Queue[tuple[dict, str | None, float]] = asyncio.Queue()
    _gen_start_t = time.time()

    async def _gen_one(item: dict) -> tuple[dict, str | None, float, str]:
        prompt = item.get("prompt", "")
        size = item.get("size", _ratio_to_size(item.get("ratio", "3:4")))
        t0 = time.time()
        wid = item.get("work_item_id", "?")
        print(f"[{wid}] 📤 请求发出 size={size} model={_plan_first} refs={len(refs)} "
              f"prompt({len(prompt)}chars)={prompt[:60]}...", flush=True)
        try:
            result = await generate_single_image(
                prompt=prompt, size=size, ref_data_uris=refs,
            )
            dt = time.time() - t0
            url = result.data_uri or result.url
            model_label = result.model or _plan_first
            if url:
                item["status"] = "done"
                return (item, url, dt, model_label)
            else:
                item["status"] = "failed"
                item["error"] = "API 返回无有效图片"
                return (item, None, dt, model_label)
        except Exception as exc:
            dt = time.time() - t0
            item["status"] = "failed"
            item["error"] = str(exc)
            return (item, None, dt, "(失败)")

    async def _worker(idx: int) -> None:
        wid = f"worker-{idx}"
        active = 0
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                if active == 0:
                    print(f"[{wid}] 🛑 启动后无任务，退出", flush=True)
                else:
                    print(f"[{wid}] ⏸️ 队列空（剩余 {queue.qsize()}），退出 —— 累计处理 {active} 张", flush=True)
                return
            shot = item.get("work_item_id", "?")
            active += 1
            item, url, dt, model_used = await _gen_one(item)
            if url:
                print(f"[{wid}] ✅ {shot} 完成 {dt:.1f}s model={model_used}  队列剩余 {queue.qsize()}", flush=True)
            else:
                print(f"[{wid}] ❌ {shot} 失败 {dt:.1f}s model={model_used} — {item.get('error', '未知错误')[:120]}", flush=True)
            await result_q.put((item, url, dt))

    print(f"[node4] 🔀 启动 {min(WORKERS, len(pending_items))} 个 worker "
          f"(总入队 {len(pending_items)})", flush=True)
    workers = [asyncio.create_task(_worker(i)) for i in range(min(WORKERS, len(pending_items)))]
    n_done = 0
    n_failed = 0
    n_total = len(pending_items)
    failed_items: list[dict[str, Any]] = []

    for _ in range(n_total):
        item, url, dt = await result_q.get()
        shot = item.get("work_item_id", "?")
        if url:
            n_done += 1
            outputs.append({
                "work_item_id": item["work_item_id"],
                "prompt_index": item.get("prompt_index", 0),
                "prompt": item.get("prompt", ""),
                "image_url": url,
            })
            _eb(task_id, "node4_image_done", {
                "work_item_id": item["work_item_id"],
                "prompt_index": item.get("prompt_index", 0),
                "prompt": item.get("prompt", ""),
                "image_url": url,
                "elapsed": round(dt, 1),
                "n_done": n_done,
                "n_total": n_total,
            })
        else:
            n_failed += 1
            if not item.get("error"):
                item["error"] = "生成异常"
            err = item.get("error", "")
            failed_items.append({
                "work_item_id": item["work_item_id"],
                "prompt_index": item.get("prompt_index", 0),
                "prompt": item.get("prompt", ""),
                "error": err,
            })
            _eb(task_id, "node4_image_failed", {
                "work_item_id": item["work_item_id"],
                "prompt_index": item.get("prompt_index", 0),
                "prompt": item.get("prompt", ""),
                "error": err,
                "elapsed": round(dt, 1),
                "n_done": n_done,
                "n_total": n_total,
            })
        print(f"[队列] {n_done + n_failed}/{n_total}  shot={shot} "
              f"耗时 {dt:.1f}s  ✅={n_done}  ❌={n_failed}", flush=True)

    await asyncio.gather(*workers)

    t_all = time.time() - _gen_start_t
    throughput = n_total / t_all if t_all > 0 else 0
    print(f"[node4] 🏁 run_gen 完成 task={task_id[:8]}: "
          f"成功 {n_done}/{n_total}, 失败 {n_failed} 张, "
          f"总耗时 {t_all:.1f}s, 吞吐 {throughput:.2f} 张/s", flush=True)

    # 汇总 phase（原 _archive 的语义，合并到这里）
    if task_id:
        _eb(task_id, "phase", {"phase": "node4_archive"})

    node4_out: dict[str, Any] = dict(node4_in)
    node4_out["outputs"] = outputs
    node4_out["failed_items"] = failed_items
    print(f"[node4] _archive: {len(outputs)} 个 outputs 已归档", flush=True)
    return {"phase": "node4_archive", "node4": node4_out}
