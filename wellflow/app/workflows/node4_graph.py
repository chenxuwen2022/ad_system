"""Node 4 子图：Node 3 的 generate_prompts 数组 → 调 LLM /images/generations → 归档 outputs。

三源输入：
  - request.product_images: Node 1 用户上传的商品图（文件路径列表）
  - node3.model_images:     C1 interrupt 用户上传的模特图（文件路径列表，可选）
  - node3.generate_prompts: Node 3 prompt_generation 产出的 prompt 数组（已按 C2
                            per_scheme_count 展开成多份变体 prompt）
  - node3.per_prompt_size:  C3 interrupt 用户选的每套 prompt 的图片规格（如 ["3:4", "3:4"]）

完整流程：
  1. _prepare:  收集商品图+模特图路径 → 读 generate_prompts → 一 prompt 一 work_item
  2. _run_gen:  并发逐 work_item 独立调 LLM /images/generations（n 强制 =1）→ data URI
  3. _archive:  归档 outputs

⚠️ 新语义：per_prompt_count 已彻底删除。Node4 不再有批量 n>1 概念，
  每张图 = 一次独立 API 调用。redo/confirm 机制在 parent_graph 的 c4_review 实现。

⚠️ 模型策略：
  Node4 生图动态从 new-api 拉 image 模型列表（qwen-image-3.0 优先），
  不从前端 interrupt、chat.py 分类器、state.node3.image_model 里取模型。
  outfit.py 已有相同降级模式验证过可用。
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Node4 生图模型获取：从 new-api 指定渠道拉取（接口只返回一个模型，无需 preferred 排序）
# 兜底链 / 渠道 ID 统一在 config.py（node4_image_channel_id / node4_image_models_fallback）
# ---------------------------------------------------------------------------


async def _get_node4_image_models() -> list[str]:
    """从 new-api 渠道 node4_image_channel_id 拉取 image 能力的模型列表。

    拉取失败或返回空时，回退到 settings.node4_image_models_fallback 硬编码链。
    """
    from wellflow.app.api.model_options import fetch_model_options
    from wellflow.app.config import settings

    channel_id = settings.node4_image_channel_id
    try:
        opts = await fetch_model_options("image", channel_id=channel_id)
        models = [_short_model_name(opt.value) for opt in opts]
    except Exception as exc:
        print(f"[node4] ⚠️ 从 channel_id={channel_id} 拉 image 模型失败，用兜底链: {exc}", flush=True)
        models = []

    if not models:
        return list(settings.node4_image_models_fallback)

    return models


def _short_model_name(model: str) -> str:
    """把 'provider/xxx' 格式剥掉 provider 前缀，只保留 'xxx'。"""
    return model.split("/", 1)[1] if "/" in model else model


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
    """收集参考图路径 → 读 generate_prompts → 一 prompt 一 work_item（N 强制 =1）。"""
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
    # ✅ 新链路：per_scheme_count 已经在 Node3 被展开成多份 prompt
    # Node4 永远一个 prompt → 一个 work_item → n=1，不再调用 LLM 的 n>1 批量参数

    per_prompt_size: list[str] = node3.get("per_prompt_size") or []

    if not prompts:
        print("[node4] ⚠️ generate_prompts 为空，无法生成 work_items", flush=True)
        node4["work_items"] = []
        node4["reference_images"] = ref_paths
        return {"phase": "node4_prepare", "node4": node4}

    # ---- 组装 work_items —— 一 prompt 一 work_item ----
    work_items: list[dict[str, Any]] = []
    for pi, prompt in enumerate(prompts):
        ratio_str = per_prompt_size[pi] if pi < len(per_prompt_size) else "3:4"
        size = _ratio_to_size(ratio_str)
        work_items.append({
            "work_item_id": f"shot-{pi+1:02d}",
            "prompt_index": pi,
            "variant_index": 0,
            "prompt": prompt,
            "ratio": ratio_str,
            "size": size,
            "status": "pending",
        })

    node4["work_items"] = work_items
    node4["reference_images"] = ref_paths
    print(f"[node4] _prepare: {len(prompts)} prompts → {len(work_items)} work_items "
          f"(每 prompt 固定 n=1), 参考图路径={len(ref_paths)}, "
          f"data_uris缓存={len(all_ref_uris)}", flush=True)
    return {"phase": "node4_prepare", "node4": node4}


# ---------------------------------------------------------------------------
# 节点 2：调 LLM 生图
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
    node4 = state.get("node4", {})
    work_items = node4.get("work_items", [])
    ref_paths: list[str] = node4.get("reference_images", [])
    cached_ref_uris: list[str] = node4.get("reference_images_data_uris") or []

    pending_items = [it for it in work_items if it.get("status") in ("pending", "redo")]
    outputs: list[dict[str, Any]] = []
    from wellflow.app.config import settings
    SEM = settings.node3_gen_concurrency  # 沿用同名配置，不影响语义

    print(
        f"[node4] _run_gen: {len(pending_items)} work_items, "
        f"并发上限={SEM}, 强制 n=1",
        flush=True,
    )

    sem = asyncio.Semaphore(SEM)
    _gen_start_t = time.time()

    async def _gen_one(item: dict) -> tuple[dict, str | None, float]:
        prompt = item.get("prompt", "")
        size = item.get("size", _ratio_to_size(item.get("ratio", "3:4")))
        async with sem:
            t0 = time.time()
            wid = item["work_item_id"]
            print(f"[{wid}] 📤 开始生图 size={size} prompt({len(prompt)}chars)={prompt[:80]}...", flush=True)
            try:
                # ✅ 关键：不传 image_model 覆盖，让 _execute_single_image 走硬编码降级链
                result = await _execute_single_image(
                    prompt=prompt, size=size,
                    ref_paths=ref_paths,
                    cached_ref_uris=cached_ref_uris,
                )
                dt = time.time() - t0
                url = result.data_uri or result.url
                if url:
                    print(f"[{wid}] ✅ 成功 — {dt:.1f}s", flush=True)
                    item["status"] = "done"
                    return (item, url, dt)
                else:
                    print(f"[{wid}] ❌ 返回但无有效 data_uri/url", flush=True)
                    item["status"] = "failed"
                    item["error"] = "API 返回无有效图片"
                    return (item, None, dt)
            except Exception as exc:
                dt = time.time() - t0
                print(f"[{wid}] ❌ 异常 — {dt:.1f}s — {exc}", flush=True)
                item["status"] = "failed"
                item["error"] = str(exc)
                return (item, None, dt)

    tasks = [asyncio.create_task(_gen_one(it)) for it in pending_items]
    n_done = 0
    n_total = len(pending_items)
    failed_items: list[dict[str, Any]] = []

    for fut in asyncio.as_completed(tasks):
        item, url, dt = await fut
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
                item["error"] = "生成异常"
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
    print(f"\n[node4] _run_gen 完成 — {n_done}/{n_total} 成功 — "
          f"总耗时 {t_all:.1f}s", flush=True)

    node4["outputs"] = outputs
    node4["failed_items"] = failed_items
    return {"phase": "node4_generation", "node4": node4}


async def _execute_single_image(prompt: str, size: str,
                                ref_paths: list[str],
                                _image_model: str | None = None,
                                cached_ref_uris: list[str] | None = None):
    """单次生图 —— 永远 n=1，动态拉取 image 模型列表逐个尝试（qwen-image-3.0 优先）。

    签名保留 _image_model 但**不再使用**（参数来自旧链路，目前没有调用方传它）。

    全链失败时 raise RuntimeError，错误信息汇总每个模型的失败原因。
    """
    from wellflow.app.llm.factory import get_llm_client
    from wellflow.app.utils.image_store import paths_to_data_uris

    if cached_ref_uris:
        image_refs = cached_ref_uris
    elif ref_paths:
        image_refs = paths_to_data_uris(ref_paths)
    else:
        image_refs = []

    _chain = await _get_node4_image_models()
    errors: list[str] = []
    for model in _chain:
        try:
            client = get_llm_client("image", model_override=model)
            # ✅ n 强制 =1 —— 一个 prompt 对应一张最终生图
            result = await client.generate_image(
                prompt=prompt,
                size=size,
                n=1,
                response_format="b64_json",
                extra_params={"image_refs": image_refs},
            )
            return result  # 成功直接返回，不再尝试后续降级模型
        except Exception as exc:
            errors.append(f"{model}: {type(exc).__name__} — {str(exc)[:200]}")
            print(
                f"[node4] ⚠️ 模型 {model} 失败 → "
                f"{'尝试降级' if model != _chain[-1] else '降级链耗尽'}",
                flush=True,
            )

    # 全链失败 —— 汇总所有模型的错误让上层感知
    raise RuntimeError(
        f"Node4 生图失败（降级链 {_chain} 全败）: " + " | ".join(errors)
    )


# ---------------------------------------------------------------------------
# 节点 3：归档
# ---------------------------------------------------------------------------


def _archive(state: dict[str, Any]) -> dict[str, Any]:
    node4 = state.get("node4", {})
    outputs = node4.get("outputs", [])
    print(f"[node4] _archive: {len(outputs)} 个 outputs 已归档", flush=True)
    return {"phase": "node4_archive", "node4": node4}
