"""增量编辑（refine）节点 —— 替代 Node1/Node2/Node3 的完全重做。

核心思路：用户说"要改 Node1/2/3 的产出"时，不走 VLM 全量重跑（昂贵、慢、不稳定），
而是把【旧产物】+【用户修改指令】喂给纯 text LLM（固定 deepseek-v4-flash，与意图识别同模型），
LLM 做最小必要增量修改，直接产出更新后的完整产物。

调用链：
  interrupt resume(decision="refine", refine_instruction="...")
    → _cX_confirm_interrupt 识别 decision="refine"
    → 写 _refine_target + _refine_instruction 到 state
    → 路由到对应 refine 节点
    → refine 节点通过 settings.classifier_model 固定调用 deepseek-v4-flash（不进入模型池）
    → 路由回同一个 cX interrupt（用户再次确认）

⚠️ Node4 不做 refine —— Node4 是生图 API，没有可"增量修改"的文本产物，
   所以 C4 redo→node4 仍然是完全重置 work_items 的方式，保持不变。
"""

from __future__ import annotations

from wellflow.app.logging import log_message

from wellflow.app.newapi.observability import business_operation, event

import time
import json
from typing import Any

from wellflow.app.config import settings


# ---------------------------------------------------------------------------
# Node1 refine：商品识别报告 增量修改（Markdown 文本）
# ---------------------------------------------------------------------------

@business_operation("Node1/报告微调")
async def refine_node1_report(state: dict[str, Any]) -> dict[str, Any]:
    """基于用户指令对 Node1 的商品识别报告做增量修改。

    输入：state.node1.product_insight（旧报告） + state._refine_instruction（用户指令）
    输出：更新后的 node1.product_insight（完整 Markdown 报告）+ report_sections（重归一化）

    🔴 锁定守卫：若 state.node1.report_locked=True，说明报告已被用户确认并锁定，
    当前任务内不得再修改 —— 直接拒绝，返回 phase=c1_confirm 且不改动 node1 任何字段。
    """
    from wellflow.app.newapi.client_factory import get_llm_client
    from wellflow.app.prompt.registry import get_active_prompt
    from wellflow.app.event_bus import publish
    from wellflow.app.workflows.report_progress import ReportProgressStream, split_report_progress

    task_id = state.get("task_id", "")
    refine_instruction = state.get("_refine_instruction", "").strip()
    old_report: str = state.get("node1", {}).get("product_insight", "") or ""
    # 历史报告可能把识别进度误存为正文，微调时只提供真正的报告。
    old_progress, old_report = split_report_progress(old_report)

    # —— 锁定守卫（第一行就查，避免已锁定后还白白调 LLM）——
    if bool((state.get("node1") or {}).get("report_locked")):
        log_message("[refine_node1] 🛡️ node1 报告已锁定，拒绝 refine", page='对话', business='node1产品报告微调', status='记录')
        if task_id:
            publish(task_id, "phase", {"phase": "c1_confirm"})
            publish(task_id, "message", {
                "text": (
                    "产品报告已确认并锁定，本任务内无法再修改。"
                    "如需调整商品信息，请新建任务重新生成报告。"
                ),
            })
        return {
            "phase": "c1_confirm",
            "_refine_target": None,
            "_refine_instruction": None,
        }

    if not refine_instruction:
        log_message("[refine_node1] ⚠️ 没有 refine_instruction，跳过编辑", page='对话', business='node1产品报告微调', status='警告')
        return {"phase": "c1_confirm"}

    if not old_report:
        log_message("[refine_node1] ⚠️ 旧报告为空，无法编辑", page='对话', business='node1产品报告微调', status='警告')
        return {"phase": "c1_confirm"}

    if task_id:
        publish(task_id, "phase", {"phase": "node1_refining", "reset_text": True})

    client = get_llm_client("text", model_override=settings.classifier_model)

    # 多轮 refine 历史（**只取 node1 自己的**——避免 node2/node3 的 refine 指令混进来）
    from wellflow.app.workflows.state import get_node_refine_history
    refine_history: list[str] = get_node_refine_history(state, "node1")
    history_block = ""
    if len(refine_history) >= 2:
        # 倒数第一条是本轮（已经在【修改指令】里单独列），前面的才是"历史"
        prev_history = refine_history[:-1]
        history_block_lines = [f"第{i + 1}轮：{h}" for i, h in enumerate(prev_history)]
        history_block = (
            f"\n【历史 refine 指令（多轮对话中用户之前提过的修改要求，"
            f"请结合本轮指令一并理解，处理指令间的重叠/矛盾/去重）】\n"
            + "\n".join(history_block_lines)
            + "\n\n"
        )

    user_message = (
        f"【商品识别报告原文】\n{old_report}\n\n"
        f"{history_block}"
        f"【本轮修改指令】\n{refine_instruction}\n\n"
        f"请基于本轮指令（参考历史指令做去重/整合），输出更新后的完整报告。"
    )

    log_message(f"[refine_node1] 📤 stream_chat → refine report (instruction_len={len(refine_instruction)})", page='对话', business='node1产品报告微调', status='开始')
    t0 = time.time()

    # --- 流式消费（reasoning_effort="close" → 不会有 thinking 通道） ---
    full_report_parts: list[str] = []
    content_chunk_index = 0
    report_stream = ReportProgressStream()

    async for item in client.stream_chat(
        system=get_active_prompt("refine_report"),
        user=user_message,
        reasoning_effort=settings.text_reasoning_effort,
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
            # close 模式下不应该有 thinking，但防御性跳过
            continue

        _, text = report_stream.feed(text)
        if not text:
            continue
        full_report_parts.append(text)
        content_chunk_index += 1
        publish(task_id, "report_chunk", {"chunk": text, "index": content_chunk_index, "node": "node1"})

    remaining = report_stream.finish()
    if remaining:
        full_report_parts.append(remaining)
        content_chunk_index += 1
        publish(task_id, "report_chunk", {"chunk": remaining, "index": content_chunk_index, "node": "node1"})
    raw_report = "".join(full_report_parts)

    # 去除可能存在的 ```markdown / ``` 包裹
    raw_report = _strip_code_fence(raw_report)

    # 拆分：报告正文 vs 引导语（LLM 按 prompt 用 ---NEXT--- 分隔）
    new_report, next_actions = _split_next_actions(raw_report)

    from wellflow.app.prompt.report_sections import build_report_sections
    new_sections = build_report_sections(new_report) if new_report else None

    total_ts = time.time() - t0
    log_message(f"[refine_node1] ✅ 完成: stream_chat, "
          f"旧报告={len(old_report)}字 → 新报告={len(new_report)}字, 耗时={total_ts:.1f}s", page='对话', business='node1产品报告微调', status='成功')

    # —— next_actions 同样不再独立 publish SSE message，改为存入 node1 state，
    #    由 parent_graph 在 C1 interrupt 时作为 hint 下发。

    return {
        "phase": "c1_confirm",
        "node1": {
            "product_insight": new_report,
            "report_sections": new_sections,
            "next_actions": next_actions,
            # 保留 VLM 缓存图、input_analysis；refine 只改文本
            "compressed_images": state.get("node1", {}).get("compressed_images", []),
            "input_analysis": state.get("node1", {}).get("input_analysis"),
            # 微调不会重新识别图片，保留首次生成时的识别进度。
            "thinking_text": old_progress or state.get("node1", {}).get("thinking_text", ""),
        },
        "_refine_target": None,
        "_refine_instruction": None,
    }


# ---------------------------------------------------------------------------
# Node2 refine：商拍策划报告正文修改
# ---------------------------------------------------------------------------

@business_operation("Node2/方案微调")
async def refine_node2_schemes(state: dict[str, Any]) -> dict[str, Any]:
    """根据用户指令修改完整商拍报告正文。"""
    from wellflow.app.newapi.client_factory import get_llm_client
    from wellflow.app.prompt.registry import get_active_prompt
    from wellflow.app.event_bus import publish
    from wellflow.app.nodes.planning_scheme import split_scheme_reports, scheme_output_contract

    task_id = state.get("task_id", "")
    instruction = state.get("_refine_instruction", "").strip()
    node2 = state.get("node2", {}) or {}
    schemes = node2.get("schemes") or []
    reports = [s.get("report_text", "") for s in schemes if isinstance(s, dict)]
    if not instruction or not reports or any(not report for report in reports):
        return {"phase": "c2_select"}

    from wellflow.app.workflows.scheme_selection import validate_scheme_indices
    selection = state.get("_refine_selected_indices")
    scheme_count = state.get("_refine_scheme_count")
    source = state.get("_refine_scheme_source")
    initial_schemes = state.get("initial_schemes") or []
    if source is None and task_id and not initial_schemes:
        import asyncio
        from wellflow.app.workflows.scheme_selection import load_initial_schemes
        initial_schemes = await asyncio.to_thread(load_initial_schemes, task_id)
    if selection is None or scheme_count is None or source is None:
        # Older checkpoints and direct refine entry points still require an intent plan.
        from wellflow.app.llm.intent_classifier import classify, summarize_graph_state
        intent = await classify(
            instruction, has_task=True, current_node="c2",
            selected_finetuning_target="node2", graph_state_brief=summarize_graph_state({**state, "initial_schemes": initial_schemes}),
            task_id=task_id or None,
        )
        if (intent.get("intent") != "edit" or intent.get("refine_target") != "node2"
                or intent.get("edit_mode") != "refine"):
            raise ValueError("未能确定商拍方案微调范围，请明确要修改的方案和数量")
        selection = intent.get("selected_indices")
        scheme_count = intent.get("scheme_output_count")
        source = intent.get("scheme_source")
    if (not isinstance(selection, list) or not selection
            or type(scheme_count) is not int or scheme_count < 1):
        raise ValueError("意图识别未返回有效的微调方案索引和输出数量，请明确后重试")
    if source not in ("current", "initial"):
        raise ValueError("意图识别未明确方案版本，请指定当前或最初方案后重试")
    if source == "initial":
        if not initial_schemes:
            import asyncio
            from wellflow.app.workflows.scheme_selection import load_initial_schemes
            initial_schemes = await asyncio.to_thread(load_initial_schemes, task_id)
        if not initial_schemes:
            raise ValueError("找不到最初商拍方案记录，不能使用当前方案代替，请重新选择")
        schemes = initial_schemes
    indices = validate_scheme_indices(selection, len(schemes))
    source_schemes = [(i, schemes[i]) for i in indices]
    if task_id:
        publish(task_id, "phase", {"phase": "node2_refining", "reset_text": True, "scheme_count": scheme_count})
    client = get_llm_client("text", model_override=settings.classifier_model)
    parts: list[str] = []
    previous = json.dumps([
        {"source_scheme_number": i + 1, "scheme_name": s.get("scheme_name") or f"方案{i + 1}", "report_text": s["report_text"]}
        for i, s in source_schemes
    ], ensure_ascii=False)
    chunk_index = 0
    async for item in client.stream_chat(
        system=get_active_prompt("refine_plan") + scheme_output_contract(scheme_count),
        user=(f"【原商拍策划方案列表】\n{previous}\n\n"
              f"【本轮修改指令】\n{instruction}"),
        reasoning_effort=settings.text_reasoning_effort,
    ):
        text = item.get("text", "") if isinstance(item, dict) else item
        if text and (not isinstance(item, dict) or item.get("type", "content") != "thinking"):
            parts.append(text)
            if task_id:
                chunk_index += 1
                publish(task_id, "scheme_chunk", {"chunk": text, "index": chunk_index, "node": "node2"})
    updated_raw = "".join(parts).strip()
    if not updated_raw:
        raise RuntimeError("商拍策划报告修改未返回正文")
    updated_schemes = split_scheme_reports(updated_raw, expected_count=scheme_count)
    if task_id:
        publish(task_id, "scheme_chunk_done", {
            "schemes": updated_schemes, "scheme_count": scheme_count, "_final": True, "node": "node2"
        })
    return {
        "phase": "c2_select",
        "node2": {**node2, "schemes": updated_schemes, "scheme_raw": updated_raw,
                  "selected_scheme_indices": [], "per_scheme_count": []},
        "_refine_target": None,
        "_refine_instruction": None,
        "_refine_selected_indices": None,
        "_refine_scheme_count": None,
        "_refine_scheme_source": None,
        "initial_schemes": initial_schemes,
    }


# ---------------------------------------------------------------------------
# Node3 refine：提示词 增量修改（自然语言 prompt 列表）
# ---------------------------------------------------------------------------

@business_operation("Node3/提示词微调")
async def refine_node3_prompts(state: dict[str, Any]) -> dict[str, Any]:
    """基于用户指令对 Node3 的生图提示词做增量修改。

    输入：state.node3.generate_prompts（旧 prompt 列表） + state._refine_instruction
    输出：更新后的 node3.generate_prompts（新 prompt 列表）
    """
    from wellflow.app.newapi.client_factory import get_llm_client
    from wellflow.app.prompt.registry import get_active_prompt
    from wellflow.app.event_bus import publish

    task_id = state.get("task_id", "")
    refine_instruction = state.get("_refine_instruction", "").strip()
    old_prompts: list[str] = state.get("node3", {}).get("generate_prompts", []) or []
    # LLM 意图分类器返回的目标 prompt 索引 —— 例："给第一个提示词加人物居中" → ["0"]
    # node3 refine 里用来显式告诉 LLM 哪些 prompt 是本次修改目标，其余原封不动
    raw_sel = state.get("_refine_selected_indices")
    n_total = len(old_prompts)
    _target_indices: list[int] | None = None
    if raw_sel is not None:
        try:
            if isinstance(raw_sel, str):
                if raw_sel.lower() == "all":
                    _target_indices = None  # all = 不限定
                else:
                    # 兜底：单字符串 "0"
                    _target_indices = [int(raw_sel)]
            elif isinstance(raw_sel, list):
                if raw_sel and raw_sel[0] == "all":
                    _target_indices = None
                else:
                    _target_indices = []
                    for _x in raw_sel:
                        try:
                            _target_indices.append(int(_x))
                        except (TypeError, ValueError):
                            pass
            # 边界钳制：过滤掉超出范围的负数索引
            if _target_indices:
                _target_indices = [i for i in _target_indices if 0 <= i < n_total] or None
        except Exception:
            _target_indices = None
    if _target_indices is not None:
        log_message(f"[refine_node3] 🎯 目标 prompt 索引已锁定: {_target_indices}", page='对话', business='node3提示词微调', status='记录')

    if not refine_instruction:
        log_message("[refine_node3] ⚠️ 没有 refine_instruction，跳过编辑", page='对话', business='node3提示词微调', status='警告')
        return {"phase": "c3_confirm"}

    if not old_prompts:
        log_message("[refine_node3] ⚠️ 旧 prompts 为空，无法编辑", page='对话', business='node3提示词微调', status='警告')
        return {"phase": "c3_confirm"}

    if task_id:
        publish(task_id, "phase", {"phase": "node3_refining", "reset_text": True})

    client = get_llm_client("text", model_override=settings.classifier_model)
    # 给每条旧 prompt 加 [i] 序号前缀，让 LLM 清楚知道边界和总数
    _numbered_old = [f"[{i + 1}] {p}" for i, p in enumerate(old_prompts)]
    old_prompts_text = "\n---PROMPT_SEP---\n".join(_numbered_old)

    # 多轮 refine 历史（**只取 node3 自己的**——避免 node1/node2 的 refine 指令混进来）
    from wellflow.app.workflows.state import get_node_refine_history
    refine_history: list[str] = get_node_refine_history(state, "node3")
    history_block = ""
    if len(refine_history) >= 2:
        prev_history = refine_history[:-1]
        history_block_lines = [f"第{i + 1}轮：{h}" for i, h in enumerate(prev_history)]
        history_block = (
            f"\n【历史 refine 指令（多轮对话中用户之前提过的修改要求，"
            f"请结合本轮指令一并理解，处理指令间的重叠/矛盾/去重）】\n"
            + "\n".join(history_block_lines)
            + "\n\n"
        )

    # —— 目标索引显式注入，避免 LLM 自己去猜"第一个/第二条/前三条"到底指哪条 ——
    if _target_indices:
        _target_desc = "、".join(f"[{i + 1}]" for i in _target_indices)
        _target_hint = (
            f"🔴 结构化目标锁定：**本次只修改 {_target_desc} 这 {len(_target_indices)} 条 prompt**，"
            f"其他 {n_total - len(_target_indices)} 条必须原封不动返回，一字不改。"
        )
    else:
        _target_hint = f"🔴 未锁定单独目标，默认所有 {n_total} 条 prompt 都可能被修改。"

    user_message = (
        f"【原生图提示词列表】（共 {n_total} 条，每条带序号前缀 [i]）\n"
        f"{old_prompts_text}\n\n"
        f"{history_block}"
        f"【本轮修改指令】\n{refine_instruction}\n\n"
        f"请基于本轮指令（参考历史指令做去重/整合），输出更新后的完整提示词列表。\n"
        f"🔴 条数约束：**当前共 {n_total} 条 prompt，请输出恰好 {n_total} 条**。\n"
        f"🔴 输出格式：每条 prompt 单独一段，用 ---PROMPT_SEP--- 分隔。\n"
        f"{_target_hint}\n"
        f"🔴 指令明确没提到的 prompt 必须原封不动复制返回，一字不改。"
    )

    log_message(f"[refine_node3] 📤 stream_chat → refine prompts (instruction_len={len(refine_instruction)})", page='对话', business='node3提示词微调', status='开始')
    t0 = time.time()

    # --- 流式消费（reasoning_effort="close" → 不会有 thinking 通道） ---
    raw_parts: list[str] = []
    content_chunk_index = 0

    async for item in client.stream_chat(
        system=get_active_prompt("refine_image_prompt"),
        user=user_message,
        reasoning_effort=settings.text_reasoning_effort,
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
            continue

        raw_parts.append(text)
        content_chunk_index += 1
        publish(task_id, "prompt_chunk", {
            "chunk": text, "index": content_chunk_index,
            "scheme_index": -1, "scheme_name": "refine",
            "variant_index": 0, "node": "node3",
        })

    raw_text = "".join(raw_parts)
    raw_text = _strip_code_fence(raw_text)

    # 按 "---PROMPT_SEP---" 切分 → 去空 → 清洗
    new_prompts = [p.strip() for p in raw_text.split("---PROMPT_SEP---") if p.strip()]
    # 兜底：如果 LLM 没按分隔符返回（只返回了一个大段），就把整段当作一条
    if not new_prompts:
        new_prompts = [raw_text.strip()] if raw_text.strip() else old_prompts

    # LLM 输出可能也带 [i] 序号前缀（因为喂的时候带了）——统一剥掉
    import re as _re
    _idx_prefix = _re.compile(r"^\s*\[\d+\]\s*")
    new_prompts = [_idx_prefix.sub("", p).strip() for p in new_prompts]

    # ---- 🔴 代码层硬兜底：条数强制对齐 ----
    # 判断用户指令是否明确要求增删 —— 只有用户明确说了才允许条数变化
    _DELETE_KWS = ("删除", "删掉", "去掉", "移除", "减掉", "少一条", "少一条", "删一条", "删第")
    _ADD_KWS = ("增加", "添加", "新增", "加一条", "多一条", "补充一条", "再来一条", "补一条")
    _user_wants_change_quantity = any(kw in refine_instruction for kw in _DELETE_KWS + _ADD_KWS)

    if not _user_wants_change_quantity and len(new_prompts) != n_total:
        log_message(f"[refine_node3] 🛡️ 触发条数兜底：用户未提增删，"
            f"LLM 返回 {len(new_prompts)} 条 ≠ 原 {n_total} 条 → 强制对齐", page='对话', business='node3提示词微调', status='记录')
        # LLM 返回的前 M 条按顺序贴到旧列表前 M 个位置，剩余位置用旧内容原封不动回填
        _merged = list(old_prompts)   # 先复制旧列表作底稿
        for _i in range(min(len(new_prompts), n_total)):
            _merged[_i] = new_prompts[_i]
        new_prompts = _merged
        log_message(f"[refine_node3]  ✅ 对齐完成 → 最终 {len(new_prompts)} 条", page='对话', business='node3提示词微调', status='成功')
    elif _user_wants_change_quantity and len(new_prompts) != n_total:
        log_message(f"[refine_node3] ℹ️ 用户明确要求增删({len(new_prompts)}→{len(old_prompts)})，"
            f"接受 LLM 返回条数变化", page='对话', business='node3提示词微调', status='记录')
    elif abs(len(new_prompts) - n_total) > 3:
        log_message(f"[refine_node3] ⚠️ 返回条数变化较大: {n_total} → {len(new_prompts)}", page='对话', business='node3提示词微调', status='警告')

    total_ts = time.time() - t0
    log_message(f"[refine_node3] ✅ 完成: stream_chat, "
          f"旧 prompts={len(old_prompts)} → 新 prompts={len(new_prompts)}, 耗时={total_ts:.1f}s", page='对话', business='node3提示词微调', status='成功')

    # 更新 prompts_detail：保持 scheme_index/variant_index 等元信息，只替换 prompt 内容
    old_details: list[dict[str, Any]] = state.get("node3", {}).get("prompts_detail", []) or []
    new_details = _rebuild_prompts_detail(old_details, new_prompts)

    node3_state = state.get("node3", {})
    return {
        "phase": "c3_confirm",
        "node3": {
            # reference_images / ratio / image_model 等会被 LangGraph reducer 自动从 state 里保留，
            # 这里只写 refine 要改动的字段（prompt 文本 + 详情 + 清空 thinking）
            "generate_prompts": new_prompts,
            "prompts_detail": new_details,
            "prompt_raw": "\n---\n".join(new_prompts),
            "per_prompt_size": [(node3_state.get("per_prompt_size") or [])[i]
                                if i < len(node3_state.get("per_prompt_size") or []) else "3:4"
                                for i in range(len(new_prompts))],
            "thinking_text": "",
        },
        "_refine_target": None,
        "_refine_instruction": None,
        "_refine_selected_indices": None,  # 🔴 消费后清空，避免下一轮 refine 继承上一轮的锁定目标
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


def _split_next_actions(raw: str) -> tuple[str, str]:
    """按 ---NEXT--- 把 LLM 返回拆成 (正文, 引导语)。

    找不到分隔符时完整 raw 当正文，引导语返回空串（调用方走 hardcoded 兜底）。
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
