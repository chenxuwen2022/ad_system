"""Node 4 子图：Node 3 的 generate_prompts 数组 → 调 LLM /images/generations → 归档 outputs。

四源输入：
  - request.product_images: Node 1 用户上传的商品图（文件路径列表）
  - node3.model_images:     C1 interrupt 用户上传的模特图（文件路径列表，可选）
  - node3.generate_prompts: Node 3 prompt_generation VLM 产出的 prompt 数组
  - node3.per_prompt_count: C3 interrupt 用户选的每套 prompt 生成几张图（如 [3, 1]）
  - node3.per_prompt_size:  C3 interrupt 用户选的每套 prompt 的图片规格（如 ["3:4", "3:4"]）

完整流程：
  1. _prepare:  收集商品图+模特图路径 → 读 generate_prompts → work_items
  2. _run_gen:  按 prompt 分组批量调 LLM /images/generations → data URI
  3. _archive:  归档 outputs

重做/确认机制在 parent_graph 的 c4_review interrupt 节点实现。
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# 辅助函数
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
    graph.add_node("archive_outputs", _archive)

    graph.add_edge(START, "prepare_work_items")
    graph.add_edge("prepare_work_items", "run_generation")
    graph.add_edge("run_generation", "archive_outputs")
    graph.add_edge("archive_outputs", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# 节点 1：准备 work_items
# ---------------------------------------------------------------------------


async def _prepare(state: dict[str, Any]) -> dict[str, Any]:
    """收集参考图路径 → 读 generate_prompts + per_prompt_count/size → work_items。"""
    import time as _time

    node3 = state.get("node3", {})
    node4 = state.get("node4", {})
    req = state.get("request", {})

    # ---- 收集参考图路径 ----
    product_paths: list[str] = req.get("product_images") or []
    model_paths: list[str] = node3.get("model_images") or []
    ref_paths = [*product_paths, *model_paths]

    # ---- 一次性组装全部 data URIs ----
    import asyncio
    from wellflow.app.utils.image_store import paths_to_data_uris

    _t0 = _time.time()
    tasks = []
    if product_paths:
        tasks.append(asyncio.to_thread(paths_to_data_uris, product_paths))
    else:
        tasks.append(asyncio.sleep(0, result=[]))
    if model_paths:
        tasks.append(asyncio.to_thread(paths_to_data_uris, model_paths))
    else:
        tasks.append(asyncio.sleep(0, result=[]))

    product_uris, model_uris = await asyncio.gather(*tasks)
    _t1 = _time.time()
    print(f"[node4] 🖼️ data URI 转换完成：商品图 {len(product_uris)}张 + 模特图 {len(model_uris)}张 "
          f"并行耗时 {_t1 - _t0:.2f}s", flush=True)

    all_ref_uris = [*product_uris, *model_uris]
    node4["reference_images_data_uris"] = all_ref_uris

    # ---- 读 Node 3 的 generate_prompts ----
    prompts: list[str] = node3.get("generate_prompts") or []
    # 每 prompt 几张图（C3 interrupt 前端选择后写回 node3.per_prompt_count）
    per_prompt_count: list[int] = node3.get("per_prompt_count") or []
    # 每 prompt 的图片规格
    per_prompt_size: list[str] = node3.get("per_prompt_size") or []

    # 兜底：没有就默认每张 prompt 1 张、默认规格
    if not per_prompt_count:
        per_prompt_count = [1] * len(prompts)
    if not per_prompt_size:
        per_prompt_size = ["3:4"] * len(prompts)

    if not prompts:
        print("[node4] ⚠️ generate_prompts 为空，无法生成 work_items", flush=True)
        node4["work_items"] = []
        node4["reference_images"] = ref_paths
        return {"phase": "node4_prepare", "node4": node4}

    # ---- 组装 work_items ----
    work_items: list[dict[str, Any]] = []
    total = 0
    for pi, prompt in enumerate(prompts):
        n = per_prompt_count[pi] if pi < len(per_prompt_count) else 1
        ratio_str = per_prompt_size[pi] if pi < len(per_prompt_size) else "3:4"
        size = _ratio_to_size(ratio_str)
        for vi in range(n):
            wid = (
                f"shot-{pi+1:02d}"
                if n == 1
                else f"shot-{pi+1:02d}-v{vi+1}"
            )
            work_items.append({
                "work_item_id": wid,
                "prompt_index": pi,
                "variant_index": vi,
                "prompt": prompt,
                "ratio": ratio_str,
                "size": size,
                "status": "pending",
            })
            total += 1

    node4["work_items"] = work_items
    node4["reference_images"] = ref_paths
    print(f"[node4] _prepare: prompts={len(prompts)} × per_prompt_count={per_prompt_count} "
          f"→ {len(work_items)} work_items, "
          f"参考图路径={len(ref_paths)}, data_uris缓存={len(all_ref_uris)}", flush=True)
    return {"phase": "node4_prepare", "node4": node4}


# ---------------------------------------------------------------------------
# 节点 2：调 LLM 生图
# ---------------------------------------------------------------------------


async def _run_gen(state: dict[str, Any]) -> dict[str, Any]:
    """按 prompt 分组批量生图 —— 每个 prompt 一次 API 调用拿 N 张变体。"""
    import asyncio
    import time
    from collections import defaultdict

    from wellflow.app.event_bus import publish as _eb

    task_id = state.get("task_id", "")
    node3 = state.get("node3", {})
    node4 = state.get("node4", {})
    work_items = node4.get("work_items", [])
    ref_paths: list[str] = node4.get("reference_images", [])
    cached_ref_uris: list[str] = node4.get("reference_images_data_uris") or []
    image_model: str | None = node3.get("image_model")

    pending_items = [it for it in work_items if it.get("status") in ("pending", "redo")]
    outputs: list[dict[str, Any]] = []
    from wellflow.app.config import settings
    SEM = settings.node3_gen_concurrency  # 沿用同名配置，不影响语义

    # 按 prompt_index 分组
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for it in pending_items:
        groups[it.get("prompt_index", 0)].append(it)
    for pi in groups:
        groups[pi].sort(key=lambda x: x.get("variant_index", 0))

    print(f"[node4] _run_gen prompt 批量：{len(pending_items)} work_items "
          f"(并发上限={SEM}, model={image_model})", flush=True)

    sem = asyncio.Semaphore(SEM)
    _gen_start_t = time.time()

    async def _gen_prompt_batch(prompt_index: int, items: list[dict]) -> list[tuple[dict, str | None, float]]:
        first = items[0]
        prompt = first.get("prompt", "")
        size = first.get("size", _ratio_to_size(first.get("ratio", "3:4")))
        n = len(items)

        async with sem:
            t0 = time.time()
            wid_preview = items[0]["work_item_id"] + (f"...{items[-1]['work_item_id']}" if n > 1 else "")
            print(f"[{wid_preview}] 📤 开始批量生图 n={n} size={size} prompt({len(prompt)}chars)={prompt[:80]}...", flush=True)
            try:
                result = await _execute_batch_images(
                    prompt=prompt, size=size, n=n,
                    ref_paths=ref_paths, image_model=image_model,
                    cached_ref_uris=cached_ref_uris,
                )
                dt = time.time() - t0
                images = result.all_images
                n_returned = len(images)

                results = []
                for vi, item in enumerate(items):
                    if vi < n_returned:
                        img = images[vi]
                        url = img.data_uri or img.url
                        if url:
                            print(f"[{item['work_item_id']}] ✅ 成功（批量第{vi+1}张）— {dt:.1f}s", flush=True)
                            item["status"] = "done"
                            results.append((item, url, dt))
                        else:
                            print(f"[{item['work_item_id']}] ❌ 批量返回但无图（第{vi+1}张）", flush=True)
                            item["status"] = "failed"
                            item["error"] = "批量返回但该子图无有效 data_uri/url"
                            results.append((item, None, dt))
                    else:
                        print(f"[{item['work_item_id']}] ❌ API 只返回 {n_returned}/{n} 张", flush=True)
                        item["status"] = "failed"
                        item["error"] = f"API 只返回 {n_returned}/{n} 张"
                        results.append((item, None, dt))
                return results

            except Exception as exc:
                dt = time.time() - t0
                print(f"[{wid_preview}] ❌ 批量异常 — {dt:.1f}s — {exc}", flush=True)
                err_msg = str(exc)
                for it in items:
                    it["status"] = "failed"
                    it["error"] = err_msg
                return [(it, None, dt) for it in items]

    tasks = [
        asyncio.create_task(_gen_prompt_batch(pi, items))
        for pi, items in sorted(groups.items())
    ]
    n_done = 0
    n_total = len(pending_items)
    failed_items: list[dict[str, Any]] = []

    for fut in asyncio.as_completed(tasks):
        batch = await fut
        for item, url, dt in batch:
            if url:
                n_done += 1
                outputs.append({
                    "work_item_id": item["work_item_id"],
                    "prompt_index": item.get("prompt_index", 0),
                    "variant_index": item.get("variant_index", 0),
                    "prompt": item.get("prompt", ""),
                    "image_url": url,
                })
                _eb(task_id, "node4_image_done", {
                    "work_item_id": item["work_item_id"],
                    "prompt_index": item.get("prompt_index", 0),
                    "variant_index": item.get("variant_index", 0),
                    "prompt": item.get("prompt", ""),
                    "image_url": url,
                    "elapsed": round(dt, 1),
                    "n_done": n_done,
                    "n_total": n_total,
                })
            elif item.get("status") != "done":
                if not item.get("error"):
                    item["status"] = "failed"
                    item["error"] = "批量调用异常"
                err = item.get("error", "")
                failed_items.append({
                    "work_item_id": item["work_item_id"],
                    "prompt_index": item.get("prompt_index", 0),
                    "variant_index": item.get("variant_index", 0),
                    "prompt": item.get("prompt", ""),
                    "error": err,
                })
                _eb(task_id, "node4_image_failed", {
                    "work_item_id": item["work_item_id"],
                    "prompt_index": item.get("prompt_index", 0),
                    "variant_index": item.get("variant_index", 0),
                    "prompt": item.get("prompt", ""),
                    "error": err,
                    "elapsed": round(dt, 1),
                    "n_done": n_done,
                    "n_total": n_total,
                })

    t_all = time.time() - _gen_start_t
    print(f"\n[node4] _run_gen 完成（prompt 批量）— {n_done}/{n_total} 成功 — "
          f"总耗时 {t_all:.1f}s（任务数={len(tasks)}）", flush=True)

    node4["outputs"] = outputs
    node4["failed_items"] = failed_items
    return {"phase": "node4_generation", "node4": node4}


async def _execute_batch_images(prompt: str, size: str, n: int,
                                 ref_paths: list[str],
                                 image_model: str | None = None,
                                 cached_ref_uris: list[str] | None = None):
    """批量生图 —— 返回 ImageGenResult（.all_images 包含所有子图）。"""
    from wellflow.app.llm.factory import get_llm_client
    from wellflow.app.utils.image_store import paths_to_data_uris

    if cached_ref_uris:
        image_refs = cached_ref_uris
    elif ref_paths:
        image_refs = paths_to_data_uris(ref_paths)
    else:
        image_refs = []

    client = get_llm_client("image", model_override=image_model)
    result = await client.generate_image(
        prompt=prompt,
        size=size,
        n=n,
        response_format="b64_json",
        extra_params={"image_refs": image_refs},
    )
    return result


# ---------------------------------------------------------------------------
# 节点 3：归档
# ---------------------------------------------------------------------------


def _archive(state: dict[str, Any]) -> dict[str, Any]:
    node4 = state.get("node4", {})
    outputs = node4.get("outputs", [])
    print(f"[node4] _archive: {len(outputs)} 个 outputs 已归档", flush=True)
    return {"phase": "node4_archive", "node4": node4}
