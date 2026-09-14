"""Node 3 子图：Node 2 的 generate_prompts 数组 → 调 LLM /images/generations → 归档 outputs。

三源输入：
  - request.product_images: Node 1 用户上传的商品图（文件路径列表，如 uploads/taskX/p0.jpg）
  - node2.model_images:     C1 interrupt 用户上传的模特图（文件路径列表，可选）
  - node2.generate_prompts: Node 2 VLM 返回的 prompt 数组（JSON 结构）

完整流程：
  1. _prepare:  收集商品图+模特图路径 → 读 generate_prompts → work_items
  2. _run_gen:  逐个 work_item 调 LLM /images/generations → data URI（调用前路径→data URI）
  3. _archive:  归档 outputs

重做/确认机制在 parent_graph 的 c3_review interrupt 节点实现。

注意：state 里只存文件路径，reference_images 存的是路径列表，
LLM 调用前才转 data URI，interrupt 给前端展示时也转 data URI。
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _ratio_to_size(ratio: str) -> str:
    """ratio 字符串（可能带后缀）→ GPT Image /images/edits 支持的固定像素 size。

    目前只保留 GPT Image 系列，走 image_ratio_to_pixel_size_gpt 映射。
    """
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
    """收集参考图路径 → 读 generate_prompts → work_items。

    state 里存的都是文件路径，不再需要 data URI → 临时文件的转换了。
    """
    import time as _time

    node1 = state.get("node1", {})
    node2 = state.get("node2", {})
    node3 = state.get("node3", {})
    req = state.get("request", {})

    # ---- 收集参考图路径 ----
    product_paths: list[str] = req.get("product_images") or []
    model_paths: list[str] = node2.get("model_images") or []
    ref_paths = [*product_paths, *model_paths]
    ratio = node2.get("ratio", "9:16")

    # ---- 一次性组装全部 data URIs ----
    # 🔑 Node3 生图 edit 模式需要原图细节（纹理/LOGO/颜色），
    #    不能用 Node1/Node2 的压缩缓存，否则会丢失高频细节导致衣服纹路和颜色失真
    import asyncio
    from wellflow.app.utils.image_store import paths_to_data_uris

    _t0 = _time.time()

    # 🔑 商品图 + 模特图 data URI 转换**并行**执行，总耗时 ≈ max(两者)
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
    print(f"[node3] 🖼️ data URI 转换完成：商品图 {len(product_uris)}张 + 模特图 {len(model_uris)}张 "
          f"并行耗时 {_t1 - _t0:.2f}s", flush=True)

    all_ref_uris = [*product_uris, *model_uris]
    node3["reference_images_data_uris"] = all_ref_uris  # 🔑 一次性缓存，_run_gen 直接读

    # ---- 读 Node 2 的 generate_prompts ----
    prompts: list[str] = node2.get("generate_prompts") or []
    # 每个 prompt 生成几张图（默认 1，由 C2 interrupt 前端选择）
    images_per_prompt: int = int(node2.get("images_per_prompt") or 1)

    if not prompts:
        print("[node3] ⚠️ generate_prompts 为空，无法生成 work_items", flush=True)
        node3["work_items"] = []
        node3["reference_images"] = ref_paths  # 存路径，interrupt 时转 data URI 给前端
        return {"phase": "node3_prepare", "node3": node3}

    # ---- 组装 work_items：每个 prompt 重复 images_per_prompt 份 ----
    # 总 work_items 数 = len(prompts) × images_per_prompt
    work_items: list[dict[str, Any]] = []
    for pi, prompt in enumerate(prompts):
        for vi in range(images_per_prompt):
            # prompt 只有 1 张或 num=1 时，variant 后缀省略，保持兼容旧 shot-XX 命名
            wid = (
                f"shot-{pi+1:02d}"
                if images_per_prompt == 1
                else f"shot-{pi+1:02d}-v{vi+1}"
            )
            work_items.append({
                "work_item_id": wid,
                "prompt_index": pi,
                "variant_index": vi,
                "prompt": prompt,
                "ratio": ratio,
                "status": "pending",
            })

    node3["work_items"] = work_items
    node3["reference_images"] = ref_paths  # 存路径（给 parent_graph C3 interrupt 展示用）
    print(f"[node3] _prepare: prompts={len(prompts)} × images_per_prompt={images_per_prompt} "
          f"→ {len(work_items)} work_items, "
          f"参考图路径={len(ref_paths)}, data_uris缓存={len(all_ref_uris)}", flush=True)
    return {"phase": "node3_prepare", "node3": node3}


# ---------------------------------------------------------------------------
# 节点 2：调 LLM 生图
# ---------------------------------------------------------------------------


async def _run_gen(state: dict[str, Any]) -> dict[str, Any]:
    """按 prompt 分组批量生图 —— 每个 prompt 一次 API 调用拿 N 张变体。

    比逐张调用减少 HTTP round-trip（1 prompt × N variants → 1 次 API 调用），
    且 ofox 的 /images/generations 原生支持 n=N + reference_images。
    不同 prompt 之间仍用 asyncio.gather 并行（Semaphore 限流）。
    """
    import asyncio
    import time
    from collections import defaultdict

    from wellflow.app.event_bus import publish as _eb

    task_id = state.get("task_id", "")
    node3 = state.get("node3", {})
    node2 = state.get("node2", {})
    work_items = node3.get("work_items", [])
    ref_paths: list[str] = node3.get("reference_images", [])
    cached_ref_uris: list[str] = node3.get("reference_images_data_uris") or []
    image_model: str | None = node2.get("image_model")

    # 只挑 pending / redo 的 work_items
    pending_items = [it for it in work_items if it.get("status") in ("pending", "redo")]
    outputs: list[dict[str, Any]] = []
    from wellflow.app.config import settings
    SEM = settings.node3_gen_concurrency

    # ---------- 按 prompt_index 分组，组内按 variant_index 排序 ----------
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for it in pending_items:
        groups[it.get("prompt_index", 0)].append(it)
    for pi in groups:
        groups[pi].sort(key=lambda x: x.get("variant_index", 0))

    print(f"[node3] _run_gen prompt 批量：{len(pending_items)} work_items "
          f"(并发上限={SEM}, model={image_model})", flush=True)

    sem = asyncio.Semaphore(SEM)
    _gen_start_t = time.time()

    async def _gen_prompt_batch(prompt_index: int, items: list[dict]) -> list[tuple[dict, str | None, float]]:
        """一个 prompt 批次 → 一次 API 调用 → N 个结果。"""
        # 从第一个 item 拿 prompt + ratio（同 prompt 组内完全相同）
        first = items[0]
        prompt = first.get("prompt", "")
        size = _ratio_to_size(first.get("ratio", "9:16"))
        n = len(items)

        async with sem:
            t0 = time.time()
            wid_preview = items[0]["work_item_id"] + (f"...{items[-1]['work_item_id']}" if n > 1 else "")
            print(f"[{wid_preview}] 📤 开始批量生图 n={n} size={size} prompt({len(prompt)}chars)={prompt[:80]}...", flush=True)
            try:
                # 🔑 批量调用 —— 一次返回 n 张
                result = await _execute_batch_images(
                    prompt=prompt, size=size, n=n,
                    ref_paths=ref_paths, image_model=image_model,
                    cached_ref_uris=cached_ref_uris,
                )
                dt = time.time() - t0

                # result.all_images 是 ImageGenResult 列表（长度 = API 实际返回数）
                images = result.all_images
                n_returned = len(images)

                results = []
                for vi, item in enumerate(items):
                    if vi < n_returned:
                        img = images[vi]
                        url = img.data_uri or img.url
                        if url:
                            print(f"[{item['work_item_id']}] ✅ 成功（批量第{vi+1}张）— {dt:.1f}s "
                                  f"data_uri(url) len={len(url)}", flush=True)
                            item["status"] = "done"
                            results.append((item, url, dt))
                        else:
                            print(f"[{item['work_item_id']}] ❌ 批量返回但无图（第{vi+1}张）", flush=True)
                            item["status"] = "failed"
                            item["error"] = "批量返回但该子图无有效 data_uri/url"
                            results.append((item, None, dt))
                    else:
                        # API 返回数 < 请求数 —— 标记失败
                        print(f"[{item['work_item_id']}] ❌ API 只返回 {n_returned}/{n} 张", flush=True)
                        item["status"] = "failed"
                        item["error"] = f"API 只返回 {n_returned}/{n} 张"
                        results.append((item, None, dt))

                return results

            except Exception as exc:
                dt = time.time() - t0
                print(f"[{wid_preview}] ❌ 批量异常 — {dt:.1f}s — {exc}", flush=True)
                err_msg = str(exc)
                # 标记整批全部失败，带清晰 error 信息
                for it in items:
                    it["status"] = "failed"
                    it["error"] = err_msg
                return [(it, None, dt) for it in items]

    # ---------- 按 prompt 批次并发调用 ----------
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
                _eb(task_id, "node3_image_done", {
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
                # 没拿到图且状态还没被标 failed（异常分支）→ 手动标
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
                _eb(task_id, "node3_image_failed", {
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
    print(f"\n[node3] _run_gen 完成（prompt 批量）— {n_done}/{n_total} 成功 — "
          f"总耗时 {t_all:.1f}s（任务数={len(tasks)}）", flush=True)

    node3["outputs"] = outputs
    node3["failed_items"] = failed_items
    return {"phase": "node3_generation", "node3": node3}


async def _execute_batch_images(prompt: str, size: str, n: int,
                                 ref_paths: list[str],
                                 image_model: str | None = None,
                                 cached_ref_uris: list[str] | None = None):
    """批量生图 —— 返回 ImageGenResult（.all_images 包含所有子图）。"""
    from wellflow.app.llm.factory import get_llm_client
    from wellflow.app.utils.image_store import paths_to_data_uris

    # 🔑 优先用缓存，完全跳过 PIL 压缩
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
    node3 = state.get("node3", {})
    outputs = node3.get("outputs", [])
    print(f"[node3] _archive: {len(outputs)} 个 outputs 已归档", flush=True)
    return {"phase": "node3_archive", "node3": node3}
