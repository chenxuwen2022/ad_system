"""共享生图 service —— 抽自 node4_graph.py，API 端点和 LangGraph 都用它。

提供三块复用逻辑：
  1. get_image_models()      —— 从 new-api 拉 image 模型列表
  2. generate_single_image() —— 单次生图（n 强制 =1），支持整条模型链自动降级
  3. is_credits_error()      —— 供上层识别 InsufficientCreditsError 用

⚠️ 边界声明：
  - 本模块只负责「选模型 + 调一次 generate_image」，统一控制进程内生图并发和 429 重试，不管 refs 来源/产物落盘/归档。
  - 调用方队列之下再经过共享并发限制，避免多个任务的请求叠加。
  - refs 传 data URI 还是 文件路径 → 调用方自己转好 data URI 再传进来。
  - 调底层 .generate_image() 时统一走 extra_params={"image_refs": [...]} 形式，
    与 node4_graph.py 的调用风格保持一致（base.py 内部有 alias 兼容两种写法）。
"""

from __future__ import annotations

import asyncio

from wellflow.app.config import settings
from wellflow.app.llm.base import ImageGenResult, InsufficientCreditsError
from wellflow.app.llm.image_retry import generate_with_rate_limit_retry


# ─────────────────────────────────────────────────────────────────────────────
# 公共函数 1：拉 image 模型列表
# ─────────────────────────────────────────────────────────────────────────────


_task_image_models: dict[str, list[str]] = {}
_task_image_locks: dict[str, asyncio.Lock] = {}


async def get_image_models(task_id: str | None = None) -> list[str]:
    """从 new-api 渠道 node4_image_channel_id 拉取 image 能力的模型列表。

    同一 task 只拉取一次；不使用本地硬编码模型兜底。
    """
    from wellflow.app.api.model_options import fetch_model_options

    if task_id and task_id in _task_image_models:
        return list(_task_image_models[task_id])

    lock = _task_image_locks.setdefault(task_id, asyncio.Lock()) if task_id else asyncio.Lock()
    async with lock:
        if task_id and task_id in _task_image_models:
            return list(_task_image_models[task_id])
        channel_id = settings.node4_image_channel_id
        opts = await fetch_model_options("image", channel_id=channel_id)
        chain = list(dict.fromkeys(_short_model_name(opt.value) for opt in opts))
        if not chain:
            raise RuntimeError(f"New API channel_id={channel_id} 未返回 image 类型模型")
        print(f"[image-gen-service] ✅ task={task_id or '-'} 首次拉取 channel_id={channel_id} image 模型: {chain}", flush=True)
        if task_id:
            _task_image_models[task_id] = chain
        return list(chain)


def clear_task_image_models(task_id: str) -> None:
    _task_image_models.pop(task_id, None)
    _task_image_locks.pop(task_id, None)


def _short_model_name(model: str) -> str:
    """把 'provider/xxx' 格式剥掉 provider 前缀，只保留 'xxx'。"""
    return model.split("/", 1)[1] if "/" in model else model


# ─────────────────────────────────────────────────────────────────────────────
# 公共函数 2：单次生图
# ─────────────────────────────────────────────────────────────────────────────


async def generate_single_image(
    prompt: str,
    size: str,
    ref_data_uris: list[str] | None = None,
    log_id: str = "image-gen",
    task_id: str | None = None,
    models: list[str] | None = None,
) -> ImageGenResult:
    """单次生图 —— 永远 n=1，动态拉取 image 模型列表逐个尝试。

    参考图走 extra_params={"image_refs": [...]} 传入，与 node4_graph.py 风格一致。

    降级链行为：
      - 链中每个模型都尝试，中途不提前中止（包括额度不足也继续试下一个模型，
        因为 new-api 可能把不同模型路由到不同 channel，某个 channel 额度耗尽
        不代表其他 channel 也耗尽）。
      - 全链失败时：若有至少一个模型报 InsufficientCreditsError，优先透传
        原始的 InsufficientCreditsError 给上层（API 端点需要识别来提前取消
        剩余并发任务）；否则聚合所有错误抛 RuntimeError。
    """
    from wellflow.app.llm.factory import get_llm_client

    refs = list(ref_data_uris) if ref_data_uris else []

    chain = list(models) if models is not None else await get_image_models(task_id)
    errors: list[str] = []
    credits_exc: InsufficientCreditsError | None = None

    for index, model in enumerate(chain):
        print(f"[{log_id}] 🎨 开始生图 model={model} ({index + 1}/{len(chain)})", flush=True)
        try:
            client = get_llm_client("image", model_override=model)
            result = await generate_with_rate_limit_retry(
                client, log_id=log_id, task_id=task_id,
                prompt=prompt,
                size=size,
                n=1,
                response_format="b64_json",
                extra_params={"image_refs": refs},
            )
            print(f"[{log_id}] ✅ 生图成功 model={model}", flush=True)
            return result  # 成功直接返回，不再尝试后续降级模型
        except InsufficientCreditsError as exc:
            # ⚠️ 额度不足：记下来，继续试下一个模型（不同模型可能路由到不同 channel）
            errors.append(f"{model}: InsufficientCreditsError — {exc.upstream_message[:200]}")
            credits_exc = exc
            reason = f"InsufficientCreditsError: {exc.upstream_message[:200]}"
        except Exception as exc:
            errors.append(f"{model}: {type(exc).__name__} — {str(exc)[:200]}")
            reason = f"{type(exc).__name__}: {str(exc)[:200]}"

        if index + 1 < len(chain):
            print(f"[{log_id}] 🔄 模型切换 {model} → {chain[index + 1]}，原因: {reason}", flush=True)
        else:
            print(f"[{log_id}] ❌ 模型链已耗尽，最后模型={model}，原因: {reason}", flush=True)

    # 全链失败
    if credits_exc is not None:
        # 至少有一个模型额度不足 → 优先透传 InsufficientCreditsError
        raise credits_exc
    raise RuntimeError(
        f"生图失败（降级链 {chain} 全败）: " + " | ".join(errors)
    )


# ─────────────────────────────────────────────────────────────────────────────
# 公共函数 3：额度不足错误识别
# ─────────────────────────────────────────────────────────────────────────────


def is_credits_error(exc: Exception) -> bool:
    return isinstance(exc, InsufficientCreditsError)
