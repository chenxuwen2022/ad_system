"""共享生图 service —— 抽自 node4_graph.py，API 端点和 LangGraph 都用它。

提供三块复用逻辑：
  1. get_image_models()      —— 从 new-api 拉 image 模型列表 + 兜底链
  2. generate_single_image() —— 单次生图（n 强制 =1），支持整条模型链自动降级
  3. is_credits_error()      —— 供上层识别 InsufficientCreditsError 用

⚠️ 边界声明：
  - 本模块只负责「选模型 + 调一次 generate_image」，**不**管并发/refs 来源/产物落盘/归档。
  - 并发由调用方自己管（node4 用 worker queue，API 端点用 Semaphore）。
  - refs 传 data URI 还是 文件路径 → 调用方自己转好 data URI 再传进来。
  - 调底层 .generate_image() 时统一走 extra_params={"image_refs": [...]} 形式，
    与 node4_graph.py 的调用风格保持一致（base.py 内部有 alias 兼容两种写法）。
"""

from __future__ import annotations

import httpx

from wellflow.app.config import settings
from wellflow.app.llm.base import ImageGenResult, InsufficientCreditsError


# ─────────────────────────────────────────────────────────────────────────────
# 公共函数 1：拉 image 模型列表
# ─────────────────────────────────────────────────────────────────────────────


async def get_image_models() -> list[str]:
    """从 new-api 渠道 node4_image_channel_id 拉取 image 能力的模型列表。

    动态模型排在前面，配置的备用模型接在后面，避免单模型故障时无处降级。
    """
    from wellflow.app.api.model_options import fetch_model_options

    channel_id = settings.node4_image_channel_id
    try:
        opts = await fetch_model_options("image", channel_id=channel_id)
        models = [_short_model_name(opt.value) for opt in opts]
    except Exception as exc:
        print(f"[image-gen-service] ⚠️ 从 channel_id={channel_id} 拉 image 模型失败，用兜底链: {exc}", flush=True)
        models = []

    # 渠道配置可能只返回一个模型；此时仅在拉取失败时使用备用链会让
    # 上游偶发 502 直接变成整批生图失败。保留动态顺序并去重。
    return list(dict.fromkeys([*models, *settings.node4_image_models_fallback]))


def _short_model_name(model: str) -> str:
    """把 'provider/xxx' 格式剥掉 provider 前缀，只保留 'xxx'。"""
    return model.split("/", 1)[1] if "/" in model else model


async def get_image_channel_candidates(models: list[str]) -> dict[str, list[str]]:
    """读取 New API 渠道配置，供日志展示候选渠道；不将其当成实际路由结果。"""
    if not settings.newapi_admin_access_token:
        return {}

    candidates: dict[str, list[str]] = {model: [] for model in models}
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            page = 1
            while True:
                response = await client.get(
                    f"{settings.newapi_admin_base_url.rstrip('/')}/api/channel/",
                    params={"p": page, "page_size": 100},
                    headers={"Authorization": f"Bearer {settings.newapi_admin_access_token}"},
                )
                response.raise_for_status()
                body = response.json()
                if body.get("success") is not True or not isinstance(body.get("data"), dict):
                    raise ValueError("渠道列表响应格式错误")
                data = body["data"]
                items = data.get("items", [])
                if not isinstance(items, list):
                    raise ValueError("渠道列表缺少 items")
                for channel in items:
                    if not isinstance(channel, dict):
                        continue
                    if channel.get("status") != 1:
                        continue
                    configured = {_short_model_name(name.strip()) for name in (channel.get("models") or "").split(",")}
                    label = f"{channel.get('id')}:{channel.get('name') or '未命名'}"
                    for model in models:
                        if model in configured:
                            candidates[model].append(label)
                if page * 100 >= data.get("total", 0) or not items:
                    break
                page += 1
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        print(f"[image-gen-service] ⚠️ 读取候选渠道失败: {type(exc).__name__}: {exc}", flush=True)
        return {}
    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# 公共函数 2：单次生图
# ─────────────────────────────────────────────────────────────────────────────


async def generate_single_image(
    prompt: str,
    size: str,
    ref_data_uris: list[str] | None = None,
    log_id: str = "image-gen",
    channel_candidates: dict[str, list[str]] | None = None,
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

    chain = await get_image_models()
    errors: list[str] = []
    credits_exc: InsufficientCreditsError | None = None

    for index, model in enumerate(chain):
        candidates = (channel_candidates or {}).get(model, [])
        channel_info = f" 候选渠道={','.join(candidates)}" if candidates else " 候选渠道=未知"
        print(f"[{log_id}] 🎨 开始生图 model={model} ({index + 1}/{len(chain)}){channel_info}", flush=True)
        try:
            client = get_llm_client("image", model_override=model)
            result = await client.generate_image(
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
            print(f"[{log_id}] 🔄 模型切换 {model} → {chain[index + 1]}，{channel_info.strip()}，原因: {reason}", flush=True)
        else:
            print(f"[{log_id}] ❌ 模型链已耗尽，最后模型={model}，{channel_info.strip()}，原因: {reason}", flush=True)

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
