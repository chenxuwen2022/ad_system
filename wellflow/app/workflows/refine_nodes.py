"""增量编辑（refine）节点 —— 替代 Node1/Node2/Node3 的完全重做。

核心思路：用户说"要改 Node1/2/3 的产出"时，不走 VLM 全量重跑（昂贵、慢、不稳定），
而是把【旧产物】+【用户修改指令】喂给纯 text LLM（走 model_pool.chat 轮询池），
LLM 做最小必要增量修改，直接产出更新后的完整产物。

调用链：
  interrupt resume(decision="refine", refine_instruction="...")
    → _cX_confirm_interrupt 识别 decision="refine"
    → 写 _refine_target + _refine_instruction 到 state
    → 路由到对应 refine 节点
    → refine 节点调用 pool.chat()
    → 路由回同一个 cX interrupt（用户再次确认）

⚠️ Node4 不做 refine —— Node4 是生图 API（openai/gpt-image-2），没有可"增量修改"的文本产物，
   所以 C4 redo→node4 仍然是完全重置 work_items 的方式，保持不变。
"""

from __future__ import annotations

import json as json_mod
import time
from typing import Any


# ---------------------------------------------------------------------------
# Node1 refine：商品识别报告 增量修改（Markdown 文本）
# ---------------------------------------------------------------------------

async def refine_node1_report(state: dict[str, Any]) -> dict[str, Any]:
    """基于用户指令对 Node1 的商品识别报告做增量修改。

    输入：state.node1.product_insight（旧报告） + state._refine_instruction（用户指令）
    输出：更新后的 node1.product_insight（完整 Markdown 报告）+ report_sections（重归一化）
    """
    from wellflow.app.llm.model_pool import get_model_pool
    from wellflow.app.prompt.constant import REFINE_NODE1_REPORT_SYSTEM_PROMPT
    from wellflow.app.event_bus import publish

    task_id = state.get("task_id", "")
    refine_instruction = state.get("_refine_instruction", "").strip()
    old_report: str = state.get("node1", {}).get("product_insight", "") or ""

    if not refine_instruction:
        print("[refine_node1] ⚠️ 没有 refine_instruction，跳过编辑", flush=True)
        return {"phase": "c1_confirm"}

    if not old_report:
        print("[refine_node1] ⚠️ 旧报告为空，无法编辑", flush=True)
        return {"phase": "c1_confirm"}

    if task_id:
        publish(task_id, "phase", {"phase": "node1_refining"})

    pool = get_model_pool()
    user_message = (
        f"【商品识别报告原文】\n{old_report}\n\n"
        f"【修改指令】\n{refine_instruction}\n\n"
        f"请基于修改指令，输出更新后的完整报告。"
    )

    print(f"[refine_node1] 📤 chat → refine report (instruction_len={len(refine_instruction)})", flush=True)
    t0 = time.time()

    resp, used_model = await pool.chat(
        system=REFINE_NODE1_REPORT_SYSTEM_PROMPT,
        user=user_message,
        reasoning_effort="none",
        temperature=0.3,
    )
    new_report: str = resp.content or ""

    # 去除可能存在的 ```markdown / ``` 包裹
    new_report = _strip_code_fence(new_report)

    from wellflow.app.prompt.report_sections import build_report_sections
    new_sections = build_report_sections(new_report) if new_report else None

    total_ts = time.time() - t0
    print(f"[refine_node1] ✅ 完成: model={used_model}, "
          f"旧报告={len(old_report)}字 → 新报告={len(new_report)}字, 耗时={total_ts:.1f}s", flush=True)

    return {
        "phase": "c1_confirm",
        "node1": {
            "product_insight": new_report,
            "report_sections": new_sections,
            # 保留 VLM 缓存图、thinking 等，refine 只改文本
            "compressed_images": state.get("node1", {}).get("compressed_images", []),
            "input_analysis": state.get("node1", {}).get("input_analysis"),
            "thinking_text": state.get("node1", {}).get("thinking_text", ""),
        },
        "_refine_target": None,
        "_refine_instruction": None,
    }


# ---------------------------------------------------------------------------
# Node2 refine：商拍方案 增量修改（JSON schemes）
# ---------------------------------------------------------------------------

async def refine_node2_schemes(state: dict[str, Any]) -> dict[str, Any]:
    """基于用户指令对 Node2 的商拍方案做增量修改。

    输入：state.node2.schemes（旧方案 list[dict]） + state._refine_instruction
    输出：更新后的 node2.schemes + node2.scheme_raw
    """
    from wellflow.app.llm.model_pool import get_model_pool
    from wellflow.app.prompt.constant import REFINE_NODE2_SCHEMES_SYSTEM_PROMPT
    from wellflow.app.event_bus import publish

    task_id = state.get("task_id", "")
    refine_instruction = state.get("_refine_instruction", "").strip()
    old_schemes: list[dict[str, Any]] = state.get("node2", {}).get("schemes", []) or []

    if not refine_instruction:
        print("[refine_node2] ⚠️ 没有 refine_instruction，跳过编辑", flush=True)
        return {"phase": "c2_select"}

    if not old_schemes:
        print("[refine_node2] ⚠️ 旧 schemes 为空，无法编辑", flush=True)
        return {"phase": "c2_select"}

    if task_id:
        publish(task_id, "phase", {"phase": "node2_refining"})

    pool = get_model_pool()
    old_json = json_mod.dumps({"schemes": old_schemes}, ensure_ascii=False, indent=2)
    user_message = (
        f"【原商拍方案 JSON】\n{old_json}\n\n"
        f"【修改指令】\n{refine_instruction}\n\n"
        f"请基于修改指令，输出更新后的完整 JSON。"
    )

    print(f"[refine_node2] 📤 chat → refine schemes (instruction_len={len(refine_instruction)})", flush=True)
    t0 = time.time()

    resp, used_model = await pool.chat(
        system=REFINE_NODE2_SCHEMES_SYSTEM_PROMPT,
        user=user_message,
        reasoning_effort="none",
        temperature=0.3,
    )
    raw_text: str = resp.content or ""
    raw_text = _strip_code_fence(raw_text)

    # 尝试解析 JSON
    new_schemes = old_schemes  # 失败兜底保留旧方案
    try:
        parsed = _extract_json(raw_text)
        new_schemes = parsed.get("schemes", []) or old_schemes
        if not isinstance(new_schemes, list) or not new_schemes:
            print(f"[refine_node2] ⚠️ LLM 返回的 schemes 解析为空，保留旧方案", flush=True)
            new_schemes = old_schemes
    except Exception as exc:
        print(f"[refine_node2] ⚠️ JSON 解析失败: {exc}，保留旧方案", flush=True)

    total_ts = time.time() - t0
    print(f"[refine_node2] ✅ 完成: model={used_model}, "
          f"旧 schemes={len(old_schemes)} → 新 schemes={len(new_schemes)}, 耗时={total_ts:.1f}s", flush=True)

    # 保留 C2 用户选的方案索引 / 每套 count，refine 只改方案内容
    node2_state = state.get("node2", {})
    return {
        "phase": "c2_select",
        "node2": {
            "schemes": new_schemes,
            "scheme_raw": raw_text,
            "selected_scheme_indices": node2_state.get("selected_scheme_indices", []),
            "per_scheme_count": node2_state.get("per_scheme_count", []),
            "thinking_text": "",
        },
        "_refine_target": None,
        "_refine_instruction": None,
    }


# ---------------------------------------------------------------------------
# Node3 refine：提示词 增量修改（自然语言 prompt 列表）
# ---------------------------------------------------------------------------

async def refine_node3_prompts(state: dict[str, Any]) -> dict[str, Any]:
    """基于用户指令对 Node3 的生图提示词做增量修改。

    输入：state.node3.generate_prompts（旧 prompt 列表） + state._refine_instruction
    输出：更新后的 node3.generate_prompts（新 prompt 列表）
    """
    from wellflow.app.llm.model_pool import get_model_pool
    from wellflow.app.prompt.constant import REFINE_NODE3_PROMPTS_SYSTEM_PROMPT
    from wellflow.app.event_bus import publish

    task_id = state.get("task_id", "")
    refine_instruction = state.get("_refine_instruction", "").strip()
    old_prompts: list[str] = state.get("node3", {}).get("generate_prompts", []) or []

    if not refine_instruction:
        print("[refine_node3] ⚠️ 没有 refine_instruction，跳过编辑", flush=True)
        return {"phase": "c3_confirm"}

    if not old_prompts:
        print("[refine_node3] ⚠️ 旧 prompts 为空，无法编辑", flush=True)
        return {"phase": "c3_confirm"}

    if task_id:
        publish(task_id, "phase", {"phase": "node3_refining"})

    pool = get_model_pool()
    # 用 "---PROMPT_SEP---" 分隔多条 prompt，让 LLM 清晰知道边界
    old_prompts_text = "\n---PROMPT_SEP---\n".join(old_prompts)
    user_message = (
        f"【原生图提示词列表】\n{old_prompts_text}\n\n"
        f"【修改指令】\n{refine_instruction}\n\n"
        f"请基于修改指令，输出更新后的完整提示词列表。"
        f"输出格式：每条 prompt 单独一段，用 ---PROMPT_SEP--- 分隔，"
        f"保持与输入同样的条数，除非指令明确要求增删。"
    )

    print(f"[refine_node3] 📤 chat → refine prompts (instruction_len={len(refine_instruction)})", flush=True)
    t0 = time.time()

    resp, used_model = await pool.chat(
        system=REFINE_NODE3_PROMPTS_SYSTEM_PROMPT,
        user=user_message,
        reasoning_effort="none",
        temperature=0.3,
    )
    raw_text: str = resp.content or ""
    raw_text = _strip_code_fence(raw_text)

    # 按 "---PROMPT_SEP---" 切分 → 去空 → 清洗
    new_prompts = [p.strip() for p in raw_text.split("---PROMPT_SEP---") if p.strip()]
    # 兜底：如果 LLM 没按分隔符返回（只返回了一个大段），就把整段当作一条
    if not new_prompts:
        new_prompts = [raw_text.strip()] if raw_text.strip() else old_prompts
    # 如果返回条数和原来差太多（比如 LLM 合并了多条），打印警告但仍用结果
    if abs(len(new_prompts) - len(old_prompts)) > 3:
        print(f"[refine_node3] ⚠️ 返回条数变化较大: {len(old_prompts)} → {len(new_prompts)}", flush=True)

    total_ts = time.time() - t0
    print(f"[refine_node3] ✅ 完成: model={used_model}, "
          f"旧 prompts={len(old_prompts)} → 新 prompts={len(new_prompts)}, 耗时={total_ts:.1f}s", flush=True)

    # 更新 prompts_detail：保持 scheme_index/variant_index 等元信息，只替换 prompt 内容
    old_details: list[dict[str, Any]] = state.get("node3", {}).get("prompts_detail", []) or []
    new_details = _rebuild_prompts_detail(old_details, new_prompts)

    node3_state = state.get("node3", {})
    return {
        "phase": "c3_confirm",
        "node3": {
            "model_images": node3_state.get("model_images", []),
            "ratio": node3_state.get("ratio"),
            "image_model": node3_state.get("image_model"),
            "generate_prompts": new_prompts,
            "prompts_detail": new_details,
            "prompt_raw": "\n---\n".join(new_prompts),
            "per_prompt_size": node3_state.get("per_prompt_size", ["3:4"] * len(new_prompts)),
            "thinking_text": "",
        },
        "_refine_target": None,
        "_refine_instruction": None,
    }


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _strip_code_fence(text: str) -> str:
    """去掉 LLM 返回中可能带的 ```xxx ``` 包裹。"""
    t = text.strip()
    if t.startswith("```"):
        # 去掉第一行 ```json / ```markdown / ```
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1:]
        else:
            t = t[3:]
    if t.endswith("```"):
        t = t[:-3].rstrip()
    return t


def _extract_json(text: str) -> dict[str, Any]:
    """从可能带前后噪声的文本里提取第一个完整 JSON 对象。"""
    import re
    text = text.strip()
    # 1. 直接解析
    try:
        return json_mod.loads(text)
    except Exception:
        pass
    # 2. 取第一个 { 到最后一个 } 之间
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        sub = text[first:last + 1]
        return json_mod.loads(sub)
    raise ValueError("JSON 提取失败")


def _rebuild_prompts_detail(old_details: list[dict[str, Any]], new_prompts: list[str]) -> list[dict[str, Any]]:
    """用新 prompt 文本替换 old_details 里的 prompt 字段，保留 scheme_index/variant_index 等元信息。

    如果新 prompts 数量 ≠ old_details 数量，按短者取；多余的 prompt 用占位 detail。
    """
    result: list[dict[str, Any]] = []
    n = min(len(old_details), len(new_prompts))
    for i in range(n):
        detail = dict(old_details[i]) if isinstance(old_details[i], dict) else {}
        detail["prompt"] = new_prompts[i]
        # negative_prompt / prompt_detail 可能没有（refine 后不再解析 JSON 结构）
        detail.pop("negative_prompt", None)
        detail.pop("prompt_detail", None)
        detail["elapsed"] = 0.0
        result.append(detail)

    # 如果新 prompts 数量比 old_details 多，补占位 detail
    for i in range(n, len(new_prompts)):
        result.append({
            "scheme_index": -1,
            "scheme_name": "refine_added",
            "variant_index": 0,
            "variant_total": 1,
            "prompt": new_prompts[i],
            "elapsed": 0.0,
        })

    return result
