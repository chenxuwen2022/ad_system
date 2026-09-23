"""父图：WellFlow 商拍任务流水线 — 真实拓扑（V2 增量编辑版）。

子图概览：
  Node1 (子图)  — do_analyze: VLM 多模态流式识别 → Markdown 报告
  Node2 (子图)  — planning_scheme: VLM 产出完整商拍策划报告正文
  Node3 (子图)  — prompt_generation: 按选中方案循环调 VLM → N 条最终生图 prompt
  Node4 (子图)  — prepare → run_generation → archive: 并发调 LLM 生图 + 归档

增量编辑（refine）节点 —— 替代完全重做：
  node1_refine_report   纯 text LLM 增量修改 Node1 报告 → 回 C1
  node2_refine_schemes  纯 text LLM 增量修改 Node2 商拍方案 → 回 C2
  node3_refine_prompts  纯 text LLM 增量修改 Node3 生图提示词 → 回 C3

主干拓扑：
  START ──→ Node1 ──→ C1 ──→ Node2 ──→ C2 ──→ Node3 ──→ C3 ──→ Node4 ──→ C4_review ──→ finalize ──→ END
                         │        │        │        │               │
                    ┌────┴──┐ ┌──┴───┐ ┌──┴───┐ ┌──┴────┐     ┌────┴────┐
                    ↓       ↓ ↓      ↓ ↓      ↓ ↓       ↓     ↓         ↓
               n1_refine  ← confirm  n2_refine←confirm n3_refine←confirm  redo_node4
                    ↑                                ↑
                    └────────────────────────────────┴─── refine 完成后回到对应 cX interrupt

决策模式：
  decision="confirm"   → 正常流转到下一个 Node 或 finalize
  decision="refine"    → 增量编辑：走纯 text LLM 改已有产物（节省算力 & 时间）
  decision="redo"      → 仅 C4 redo→node4 保留（Node4 是生图 API，无文本产物可 refine）

HITL resume 数据：
  C1  resume: confirmed_report | decision("confirm"|"refine") + refine_instruction + reference_images/ratio
  C2  resume: selected_scheme_indices + per_scheme_count | decision("confirm"|"refine") + refine_instruction
  C3  resume: edited_prompts + selected_prompt_indices + per_prompt_size | decision("confirm"|"refine") + refine_instruction
  C4  resume: decision("confirm"|"redo") + redo_target("node4") | decision("refine") + refine_instruction + refine_target

State 临时字段：
  _refine_target: "node1"|"node2"|"node3" — refine 节点识别后自动清空
  _refine_instruction: str — 用户修改指令，refine 节点消费后自动清空
  _redo_target: str — 保留旧语义，仅 C4 redo→node4 使用

Phase 枚举（前端 PHASE_LABELS 对齐）：
  node1_input_check → node1_vlm_analyzing → node1_vlm_done → c1_confirm
  → node2_plan_scheme → c2_select → node3_prompt_gen → c3_confirm
  → node4_prepare → node4_generation → node4_archive → c4_review → done/failed
  + node1_refining / node2_refining / node3_refining（refine 中间态）

Node4 子图内部已合并为 2 节点：prepare_work_items → run_generation（含 archive 语义）
"""

from __future__ import annotations

from typing import Any
from wellflow.app.workflows.decisions import decision_node, request_interrupt


# ---------------------------------------------------------------------------
# 构建父图
# ---------------------------------------------------------------------------


def build_graph(checkpointer=None):
    try:
        from langgraph.graph import StateGraph, START, END
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "langgraph 未安装。请 pip install langgraph langgraph-checkpoint-postgres"
        ) from exc

    from wellflow.app.workflows.state import TaskState
    from wellflow.app.workflows import (
        node1_graph as _n1,
        node2_graph as _n2,
        node3_graph as _n3,
        node4_graph as _n4,
        finalize as _finalize,
        refine_nodes as _refine,
    )

    graph = StateGraph(TaskState)

    # ---- 子图 ----
    graph.add_node("node1_product_analyzer", _n1.build_graph())
    graph.add_node("node2_planning_scheme", _n2.build_graph())
    graph.add_node("node3_prompt_generation", _n3.build_graph())
    graph.add_node("node4_generate_image", _n4.build_graph())

    # ---- 增量编辑（refine）节点 —— 替代完全重做 ----
    graph.add_node("node1_refine_report", _refine.refine_node1_report)
    graph.add_node("node2_refine_schemes", _refine.refine_node2_schemes)
    graph.add_node("node3_refine_prompts", _refine.refine_node3_prompts)

    # ---- HITL interrupt 节点 ----
    graph.add_node("c1_confirm_report", _c1_confirm_report)
    graph.add_node("c2_select_scheme", _c2_select_scheme)
    graph.add_node("c3_confirm_prompt", _c3_confirm_prompt)
    graph.add_node("c4_review_result", _c4_review_result)

    # ---- finalize ----
    graph.add_node("finalize", _finalize.finalize)

    # ---- 边：线性主干 ----
    graph.add_edge(START, "node1_product_analyzer")
    graph.add_edge("node1_product_analyzer", "c1_confirm_report")

    # ---- C1 条件路由：refine → node1_refine_report；confirm(confirmations.c1=True) → Node2；
    #                  无路由信号（被拒/首 interrupt 返回空值）→ 自循环留在 C1 ----
    graph.add_conditional_edges(
        "c1_confirm_report",
        _route_c1_decision,
        {
            "node2_planning_scheme": "node2_planning_scheme",
            "node1_refine_report": "node1_refine_report",
            "c1_confirm_report": "c1_confirm_report",  # 自循环：让用户重选
        },
    )
    # refine 完成后回到 C1 让用户再次确认
    graph.add_edge("node1_refine_report", "c1_confirm_report")

    graph.add_edge("node2_planning_scheme", "c2_select_scheme")

    # ---- C2 条件路由：refine → node2_refine_schemes；confirm(confirmations.c2=True) → Node3；
    #                  无路由信号 → 自循环留在 C2 ----
    graph.add_conditional_edges(
        "c2_select_scheme",
        _route_c2_decision,
        {
            "node3_prompt_generation": "node3_prompt_generation",
            "node2_refine_schemes": "node2_refine_schemes",
            "c2_select_scheme": "c2_select_scheme",  # 自循环
        },
    )
    # refine 完回 C2 重选方案；同时承接 C3/C4 跨层级回退到 node2_refine_schemes 的场景
    graph.add_edge("node2_refine_schemes", "c2_select_scheme")

    graph.add_edge("node3_prompt_generation", "c3_confirm_prompt")

    # ---- C3 条件路由：confirm(confirmations.c3=True) → Node4；refine → node2_refine_schemes / node3_refine_prompts；
    #                  无路由信号 → 自循环留在 C3 ----
    graph.add_conditional_edges(
        "c3_confirm_prompt",
        _route_c3_decision,
        {
            "node4_generate_image": "node4_generate_image",
            "node2_refine_schemes": "node2_refine_schemes",
            "node3_refine_prompts": "node3_refine_prompts",
            "c3_confirm_prompt": "c3_confirm_prompt",  # 自循环
        },
    )
    graph.add_edge("node3_refine_prompts", "c3_confirm_prompt")

    # ---- C4 条件路由 ----
    # Node4 完成后 → 固定走 C4 review（_route_after_node4 原函数恒返回 "c4_review"，
    #                                    映射表里的 "finalize" 分支是死代码，已删除）
    graph.add_edge("node4_generate_image", "c4_review_result")
    # C4 review → finalize(confirm) 或 refine(node2/3) 或 redo(node4)；
    #             无路由信号（redo/refine 被拒）→ 自循环留在 C4 让用户重选
    graph.add_conditional_edges(
        "c4_review_result",
        _route_c4_decision,
        {
            "finalize": "finalize",
            "node2_refine_schemes": "node2_refine_schemes",
            "node3_refine_prompts": "node3_refine_prompts",
            "node4_generate_image": "node4_generate_image",  # 仅 redo→node4 保留完全重置
            "c4_review_result": "c4_review_result",  # 自循环：redo/refine 被拒时留在 C4
        },
    )

    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# HITL interrupt 节点
# ---------------------------------------------------------------------------


def _report_hash(text: str | None) -> str:
    """对报告文本做稳定哈希（FNV-1a 轻量实现）。"""
    if not text:
        return "0" * 16
    h = 0xCBF29CE484222325
    for ch in text.encode("utf-8"):
        h ^= ch
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


@decision_node(1)
def _c1_confirm_report(state: dict[str, Any]) -> dict[str, Any]:
    """C1：用户确认商品识别报告。

    interrupt 让前端展示 Node1 的 product_insight（Markdown 报告），
    用户可以多轮修改（decision="refine"）或最终确认（decision="confirm"）。

    决策模式：
      decision="confirm"  → 写入 report_locked=True / report_hash / confirmed_at，
                            confirmations.c1=True，然后流转到 Node2。
                            版本绑定：interrupt_value 若带 report_hash，必须与 state
                            当前 product_insight 哈希一致，否则视为旧版本 → 拒绝并留在 C1。
                            重复确认（report_locked 已是 True）→ 直接返回空更新，
                            让 graph 走 confirm 分支到 Node2（不重复写入锁字段）。
      decision="refine"   → 纯 text LLM 增量编辑报告（不走 VLM 重跑）
                            需配合 refine_instruction（用户的修改指令）
    """
    from langgraph.types import interrupt
    from wellflow.app.event_bus import publish

    task_id = state.get("task_id", "")
    node1_state = state.get("node1", {}) or {}
    current_report = str(node1_state.get("product_insight", "") or "")
    current_hash = _report_hash(current_report)

    # 优先使用 LLM 动态生成的 next_actions（报告 ---NEXT--- 分隔线下的引导语）；
    # 没有则用 hardcoded 兜底
    next_actions = str(node1_state.get("next_actions", "") or "").strip()
    fallback_hint = (
        "请确认商品识别报告，可继续修改或确认进入下一步。"
        "确认后本任务内报告将锁定，无法再修改。"
    )
    c1_hint = next_actions if next_actions else fallback_hint

    if task_id:
        publish(task_id, "phase", {"phase": "c1_confirm"})

    # —— 首次/每次 refine 后进入 C1，都要下发最新版本的 hash，
    #    让前端在 confirm 时原样带回，防止用户确认旧版本报告 ——
    interrupt_value = request_interrupt(state, {
        "node": "c1",
        "phase": "c1_confirm",
        "hint": c1_hint,
        "report": current_report,
        "report_sections": node1_state.get("report_sections"),
        "report_hash": current_hash,
        "report_locked": bool(node1_state.get("report_locked")),
        "thinking_text": node1_state.get("thinking_text", ""),
    })
    print(f"[c1_confirm_report] 🎯 interrupt_value preview: report={current_report[:200]!r}, "
          f"sections_keys={list((node1_state.get('report_sections') or {}).keys()) if isinstance(node1_state.get('report_sections'), dict) else 'N/A'}, "
          f"locked={bool(node1_state.get('report_locked'))}", flush=True)

    if not interrupt_value:
        return {"phase": "c1_confirm"}

    # —— 若已锁定（重复 confirm / stale 请求）→ 走 confirm 分支直接下一个节点，
    #    但保持不改动 node1 的任何字段 ——
    if bool(node1_state.get("report_locked")):
        print(f"[c1_confirm_report] ⚠️ node1 已锁定，忽略重复 confirm 值，直接放行到 Node2", flush=True)
        return {
            "phase": "c1_confirm",
            # confirmations.c1 仍要 True（保证 downstream 不回推）
            "confirmations": {"c1": True},
            "_redo_target": None,
        }

    decision = interrupt_value.get("decision", "confirm")

    # ---- refine：增量编辑报告（替代原来的完全重做）----
    if decision == "refine":
        refine_instruction = interrupt_value.get("refine_instruction", "").strip()
        if not refine_instruction:
            print("[c1_confirm_report] ⚠️ refine 但无 refine_instruction，拒绝", flush=True)
            return {"phase": "c1_confirm"}
        print(f"[c1_confirm_report] 🔧 refine → node1 报告, instruction={refine_instruction}", flush=True)
        # 追加到多轮 refine 历史 —— refine 节点会用它做指令整合（**按 node1 隔离**）
        from wellflow.app.workflows.state import build_refine_history_update
        prev_history = state.get("_refine_history") if isinstance(state, dict) else None
        return {
            "phase": "c1_confirm",
            "_refine_target": "node1",
            "_refine_instruction": refine_instruction,
            "_refine_history": build_refine_history_update(prev_history, "node1", refine_instruction),
            "_redo_target": None,
        }

    # ---- confirm：版本绑定 + 锁定 ----
    new_node1 = dict(node1_state)
    new_node3 = {k: v for k, v in state.get("node3", {}).items()
                 if k in ("reference_images", "ratio", "image_model")}

    # 允许前端传入用户编辑后的 confirmed_report —— 若与 state 不同，需重新计算 hash
    incoming_report = interrupt_value.get("confirmed_report")
    if incoming_report is not None and (not isinstance(incoming_report, str) or not incoming_report.strip()):
        return {"phase": "c1_confirm"}
    incoming_hash = interrupt_value.get("report_hash")

    # 优先使用 state 里最新的报告做版本绑定（不是用户传来的 confirmed_report）。
    # 若 hash 不一致 → 说明 refine 已改了报告，但用户还拿着旧版本在点确认 → 拒绝。
    if incoming_hash is not None and incoming_hash != current_hash:
        print(
            f"[c1_confirm_report] 🛡️ report_hash 版本不匹配："
            f"user_sent={incoming_hash[:8]} vs state_current={current_hash[:8]} → 拒绝确认，留在 C1",
            flush=True,
        )
        # 直接留在 C1，让用户看到最新报告版本
        return {"phase": "c1_confirm"}

    if incoming_report is not None and incoming_report != current_report:
        # 用户在前端手工编辑了报告正文 → 用用户的版本，并重新算 hash 作为锁定依据
        new_node1["product_insight"] = incoming_report
        from wellflow.app.prompt.report_sections import build_report_sections
        new_node1["report_sections"] = build_report_sections(incoming_report)
        current_hash = _report_hash(incoming_report)

    import time as _time
    new_node1["report_locked"] = True
    new_node1["report_hash"] = current_hash
    new_node1["confirmed_at"] = _time.time()

    print(
        f"[c1_confirm_report] ✅ node1 报告已锁定: hash={current_hash[:8]} "
        f"len={len(new_node1.get('product_insight', ''))}",
        flush=True,
    )

    # —— 参考图：C1 也可以上传 mannequin 图（可选），scene/outfit 留到 C2 ——
    new_refs = dict(new_node3.get("reference_images") or {"mannequin": [], "scene": [], "outfit": []})
    incoming_refs = interrupt_value.get("reference_images") or {}
    if "mannequin" in incoming_refs:
        new_refs["mannequin"] = list(incoming_refs["mannequin"])
    new_node3["reference_images"] = new_refs

    ratio = interrupt_value.get("ratio")
    if ratio:
        new_node3["ratio"] = ratio

    image_model = interrupt_value.get("image_model")
    if image_model:
        new_node3["image_model"] = image_model

    return {"phase": "c1_confirm", "node1": new_node1, "node3": {"__clear__": True, **new_node3},
            "confirmations": {"c1": True}, "_redo_target": None}


@decision_node(2)
def _c2_select_scheme(state: dict[str, Any]) -> dict[str, Any]:
    """C2：用户明确选定一套商拍方案，或继续微调方案。

    决策模式：
      decision="confirm"  → 校验选中索引，写入每套提示词数量后流转到 Node3
      decision="refine"   → 纯 text LLM 增量修改商拍方案（不走 VLM 重跑）
    """
    from langgraph.types import interrupt
    from wellflow.app.event_bus import publish

    task_id = state.get("task_id", "")
    node3 = state.get("node3", {})

    if task_id:
        publish(task_id, "phase", {"phase": "c2_select"})

    interrupt_value = request_interrupt(state, {
        "node": "c2",
        "phase": "c2_select",
        "hint": "请从商拍策划方案列表中选定一套。确认后将按前端设置生成差异化生图提示词",
        "schemes": state.get("node2", {}).get("schemes", []),
        "scheme_raw": state.get("node2", {}).get("scheme_raw", ""),
        "reference_images": node3.get("reference_images", {"mannequin": [], "scene": [], "outfit": []}),
        "thinking_text": state.get("node2", {}).get("thinking_text", ""),
    })

    if not interrupt_value:
        return {"phase": "c2_select"}

    decision = interrupt_value.get("decision", "confirm")

    # ---- refine：增量编辑 Node2 商拍方案 ----
    if decision == "refine":
        refine_instruction = interrupt_value.get("refine_instruction", "").strip()
        if not refine_instruction:
            print("[c2_select] ⚠️ refine 但无 refine_instruction，拒绝", flush=True)
            return {"phase": "c2_select"}
        # 来自 LLM 意图分类器：决定 refine LLM 能看到哪几套方案
        # 格式兼容：LLM 可能输出 ["0","1"] 或 "all"，后面 refine_node2_schemes 会统一解析
        _refine_sel_idx = interrupt_value.get("refine_selected_indices")
        print(f"[c2_select] 🔧 refine → node2 商拍方案, instruction={refine_instruction}, "
              f"refine_selected_indices={_refine_sel_idx}", flush=True)
        from wellflow.app.workflows.state import build_refine_history_update
        prev_history = state.get("_refine_history") if isinstance(state, dict) else None
        return {
            "phase": "c2_select",
            "_refine_target": "node2",
            "_refine_instruction": refine_instruction,
            "_refine_selected_indices": _refine_sel_idx,
            "_refine_history": build_refine_history_update(prev_history, "node2", refine_instruction),
            "_redo_target": None,
        }

    # ---- confirm：多套时必须明确选中；单套时可直接确认 ----
    node2 = state.get("node2", {})
    selected = interrupt_value.get("selected_scheme_indices")
    new_node2 = dict(node2)
    all_schemes = node2.get("schemes", [])

    # 单套方案可直接确认；多套方案仍须明确选中。
    if selected is None and len(all_schemes) == 1:
        selected = [0]
    if (not isinstance(selected, list) or len(selected) != 1
            or type(selected[0]) is not int
            or not 0 <= selected[0] < len(all_schemes)):
        print(f"[c2_select] ⚠️ 无效方案选择: {selected!r}", flush=True)
        return {"phase": "c2_select"}
    new_node2["selected_scheme_indices"] = selected

    # 提示词数量完全由前端随选中方案提交；后端不设置默认值。
    counts = interrupt_value.get("per_scheme_count")
    n_selected = len(new_node2["selected_scheme_indices"])
    if (not isinstance(counts, list) or len(counts) != n_selected
            or any(type(c) is not int or c < 1 for c in counts)):
        print(f"[c2_select] ⚠️ 无效 per_scheme_count: {counts!r}", flush=True)
        return {"phase": "c2_select"}
    new_node2["per_scheme_count"] = counts
    print(f"[c2_select] ✅ selected={new_node2['selected_scheme_indices']} "
          f"per_scheme_count={new_node2['per_scheme_count']}", flush=True)

    new_node3 = {k: v for k, v in state.get("node3", {}).items()
                 if k in ("reference_images", "ratio", "image_model")}
    ratio = interrupt_value.get("ratio")
    if ratio:
        new_node3["ratio"] = ratio
    image_model = interrupt_value.get("image_model")
    if image_model:
        new_node3["image_model"] = image_model
    incoming_refs = interrupt_value.get("reference_images") or {}
    new_node3["reference_images"] = {
        "mannequin": list(incoming_refs.get("mannequin", new_node3.get("reference_images", {}).get("mannequin", []))),
        "scene": list(incoming_refs.get("scene", new_node3.get("reference_images", {}).get("scene", []))),
        "outfit": list(incoming_refs.get("outfit", new_node3.get("reference_images", {}).get("outfit", []))),
    }

    return {"phase": "c2_select", "node2": new_node2, "node3": {"__clear__": True, **new_node3},
            "confirmations": {"c2": True}, "_redo_target": None}


@decision_node(3)
def _c3_confirm_prompt(state: dict[str, Any]) -> dict[str, Any]:
    """C3：用户确认每套 prompt 的最终内容 + 选规格。

    interrupt 让前端展示 Node3 的 generate_prompts + prompts_detail。

    决策模式：
      decision="confirm"  → 正常流转到 Node4 生图
      decision="refine"   → 纯 text LLM 增量编辑。refine_target 决定改哪个：
                              node3 → 改当前提示词列表（走 node3_refine_prompts → 回 C3）
                              node2 → 改商拍方案（走 node2_refine_schemes → 回 C2 重选方案）
                            refine_target 默认为 "node3"
    """
    from langgraph.types import interrupt
    from wellflow.app.event_bus import publish

    task_id = state.get("task_id", "")

    if task_id:
        publish(task_id, "phase", {"phase": "c3_confirm"})

    interrupt_value = request_interrupt(state, {
        "node": "c3",
        "phase": "c3_confirm",
        "hint": "请确认每套方案的最终提示词，可编辑后继续生图",
        "generate_prompts": state.get("node3", {}).get("generate_prompts", []),
        "prompts_detail": state.get("node3", {}).get("prompts_detail", []),
        "reference_images": state.get("node3", {}).get("reference_images", {"mannequin": [], "scene": [], "outfit": []}),
        "thinking_text": state.get("node3", {}).get("thinking_text", ""),
    })

    if not interrupt_value:
        return {"phase": "c3_confirm"}

    decision = interrupt_value.get("decision", "confirm")

    # ---- refine：增量编辑（node2 或 node3）----
    if decision == "refine":
        refine_instruction = interrupt_value.get("refine_instruction", "").strip()
        refine_target = interrupt_value.get("refine_target", "node3")
        if refine_target not in ("node2", "node3"):
            refine_target = "node3"
        if not refine_instruction:
            print("[c3_confirm] ⚠️ refine 但无 refine_instruction，拒绝", flush=True)
            return {"phase": "c3_confirm"}
        # node2 refine：方案过滤索引；node3 refine：也透传 selected_indices
        # （"把第1条提示词加人物居中" → selected_indices=["0"]，refine_node3 会用它限定目标）
        _refine_sel_idx = interrupt_value.get("refine_selected_indices")
        print(f"[c3_confirm] 🔧 refine → {refine_target}, instruction={refine_instruction}, "
              f"refine_selected_indices={_refine_sel_idx}", flush=True)
        from wellflow.app.workflows.state import build_refine_history_update
        prev_history = state.get("_refine_history") if isinstance(state, dict) else None
        return {
            "phase": "c3_confirm",
            "_refine_target": refine_target,
            "_refine_instruction": refine_instruction,
            "_refine_selected_indices": _refine_sel_idx,
            "_refine_history": build_refine_history_update(prev_history, refine_target, refine_instruction),
            "_redo_target": None,
        }

    # ---- confirm：正常处理 prompt 编辑 + 规格 + 选中过滤 ----
    new_node3 = dict(state.get("node3", {}))

    from wellflow.app.workflows.refine_nodes import _rebuild_prompts_detail
    edited = interrupt_value.get("edited_prompts")
    prompts = edited if edited is not None else new_node3.get("generate_prompts", [])
    if not isinstance(prompts, list) or not prompts or any(not isinstance(x, str) or not x.strip() for x in prompts):
        return {"phase": "c3_confirm"}
    sizes = interrupt_value.get("per_prompt_size", new_node3.get("per_prompt_size") or [])
    if not isinstance(sizes, list):
        return {"phase": "c3_confirm"}
    sizes = [sizes[i] if i < len(sizes) and isinstance(sizes[i], str) and sizes[i] else "3:4"
             for i in range(len(prompts))]
    selected = interrupt_value.get("selected_prompt_indices", list(range(len(prompts))))
    if (not isinstance(selected, list) or not selected
            or any(type(i) is not int or i < 0 or i >= len(prompts) for i in selected)):
        return {"phase": "c3_confirm"}
    selected = sorted(set(selected))
    details = _rebuild_prompts_detail(new_node3.get("prompts_detail") or [], prompts)
    new_node3["generate_prompts"] = [prompts[i] for i in selected]
    new_node3["prompts_detail"] = [details[i] for i in selected]
    new_node3["per_prompt_size"] = [sizes[i] for i in selected]
    new_node3["prompt_raw"] = "\n---\n".join(new_node3["generate_prompts"])

    ratio = interrupt_value.get("ratio")
    if ratio:
        new_node3["ratio"] = ratio
    image_model = interrupt_value.get("image_model")
    if image_model:
        new_node3["image_model"] = image_model
    incoming_refs = interrupt_value.get("reference_images") or {}
    new_node3["reference_images"] = {
        "mannequin": list(incoming_refs.get("mannequin", new_node3.get("reference_images", {}).get("mannequin", []))),
        "scene": list(incoming_refs.get("scene", new_node3.get("reference_images", {}).get("scene", []))),
        "outfit": list(incoming_refs.get("outfit", new_node3.get("reference_images", {}).get("outfit", []))),
    }

    return {"phase": "c3_confirm", "node3": new_node3,
            "confirmations": {"c3": True}, "_redo_target": None}


@decision_node(4)
def _c4_review_result(state: dict[str, Any]) -> dict[str, Any]:
    """C4：用户查看生图结果。

    决策模式：
      decision="confirm"  → 进 finalize 归档
      decision="redo" + redo_target="node4" → 无论上一轮是否失败，都重新生成全部图片
                                               Node4 是生图 API，没有可"增量编辑"的文本产物
      decision="refine"   → 纯 text LLM 增量编辑。refine_target 为 "node2" 或 "node3"
    """
    from langgraph.types import interrupt
    from wellflow.app.event_bus import publish

    task_id = state.get("task_id", "")
    node1 = state.get("node1", {})
    node2 = state.get("node2", {})
    node3 = state.get("node3", {})
    node4 = state.get("node4", {})
    req = state.get("request", {})

    if task_id:
        publish(task_id, "phase", {"phase": "c4_review"})

    interrupt_value = request_interrupt(state, {
        "node": "c4",
        "phase": "c4_review",
        "hint": node4.get("generation_summary", ""),
        "generation_status": node4.get("generation_status"),
        "outputs": node4.get("outputs", []),
        "failed_items": node4.get("failed_items", []),
        "reference_images": req.get("product_images", []),
        "reference_images_node3": node3.get("reference_images", {"mannequin": [], "scene": [], "outfit": []}),
        "generate_prompts": node3.get("generate_prompts", []),
        "thinking_text": node4.get("thinking_text", ""),
    })

    if not interrupt_value:
        return {"phase": "c4_review"}

    decision = interrupt_value.get("decision", "confirm")

    # ---- confirm → finalize ----
    if decision == "confirm":
        return {"phase": "c4_review", "confirmations": {"c4": True}, "_redo_target": None}

    # ---- refine → 增量编辑 node2 或 node3 ----
    if decision == "refine":
        refine_target = interrupt_value.get("refine_target", "node3")
        refine_instruction = interrupt_value.get("refine_instruction", "").strip()
        if refine_target not in ("node2", "node3"):
            refine_target = "node3"
        if not refine_instruction:
            print("[c4_review] ⚠️ refine 但无 refine_instruction，拒绝", flush=True)
            return {"phase": "c4_review"}
        # 透传意图分类器的 selected_indices —— node2 用作方案过滤；node3 用作 prompt 目标限定
        _refine_sel_idx = interrupt_value.get("refine_selected_indices")
        print(f"[c4_review] 🔧 refine → {refine_target}, instruction={refine_instruction}, "
              f"refine_selected_indices={_refine_sel_idx}", flush=True)
        from wellflow.app.workflows.state import build_refine_history_update
        prev_history = state.get("_refine_history") if isinstance(state, dict) else None
        return {
            "phase": "c4_review",
            "_refine_target": refine_target,
            "_refine_instruction": refine_instruction,
            "_refine_selected_indices": _refine_sel_idx,
            "_refine_history": build_refine_history_update(prev_history, refine_target, refine_instruction),
            "_redo_target": None,
        }

    # ---- redo → 仅保留 node4（生图 API，无文本产物可增量编辑）----
    redo_target = interrupt_value.get("redo_target", "node4")
    if redo_target != "node4":
        # 🔴 显式拒绝而不是静默降级：
        #   node1 在 C1 确认后已 report_locked=True，本任务内绝对改不动；
        #   node2/node3 有 refine 增量编辑通道，redo（完全重置）对 text 产物没意义。
        # 留在 C4 让用户重选 confirm / refine / redo→node4。
        _msg_map = {
            "node1": "商品识别报告已确认并锁定，本任务内无法重新生成。如需调整商品信息，请新建任务。",
            "node2": "商拍方案支持增量 refine（修改指令 + refine），不支持完全重做。请选择 refine 或确认。",
            "node3": "生图提示词支持增量 refine（修改指令 + refine），不支持完全重做。请选择 refine 或确认。",
        }
        _msg = _msg_map.get(redo_target, f"redo→{redo_target} 不支持完全重做，请选择 refine 或确认。")
        print(f"[c4_review] 🛡️ redo→{redo_target} 被拒: {_msg}", flush=True)
        if task_id:
            publish(task_id, "message", {"text": _msg})
        return {
            "phase": "c4_review",
            "_redo_target": None,
            "_refine_target": None,
            "_refine_instruction": None,
            "_refine_selected_indices": None,
        }

    from wellflow.app.workflows.node4_graph import prepare_image_redo
    new_node4 = prepare_image_redo(node4)
    return {
        "phase": "c4_review",
        "node1": node1,
        "node2": node2,
        "node3": node3,
        "node4": new_node4,
        "_redo_target": "node4",
        "_refine_target": None,
        "_refine_instruction": None,
        "_refine_selected_indices": None,  # 🛑 清掉上一次 refine 残留的方案过滤索引
    }


# ---------------------------------------------------------------------------
# 条件路由
# ---------------------------------------------------------------------------


def _node_mapping() -> dict[str, str]:
    """refine_target / redo_target → 父图节点名 的统一映射。

    仅用于 _route_c4_decision 中的 redo→node4 路径；
    C1/C2/C3/C4 的 refine 路径由各 _route_cX_decision 函数单独处理。
    """
    return {
        "node1": "node1_refine_report",
        "node2": "node2_refine_schemes",
        "node3": "node3_refine_prompts",
        "node4": "node4_generate_image",  # 唯一保留的完全重做（生图 API）
    }


def _route_c1_decision(state: dict[str, Any]) -> str:
    """C1 interrupt resume 后的路由：
      - refine_target=node1 → node1_refine_report
      - confirmations.c1=True → Node2（正常流转）
      - 其他（refine 被拒 / interrupt 首次返回空值等）→ 留在 C1 让用户重选
    """
    refine_target = state.get("_refine_target")
    if refine_target == "node1":
        return _node_mapping()["node1"]
    confirmations = state.get("confirmations") or {}
    if confirmations.get("c1"):
        return "node2_planning_scheme"
    return "c1_confirm_report"


def _route_c2_decision(state: dict[str, Any]) -> str:
    """C2 interrupt resume 后的路由：
      - refine_target=node2 → node2_refine_schemes
      - confirmations.c2=True → Node3
      - 其他 → 留在 C2 让用户重选
    """
    refine_target = state.get("_refine_target")
    if refine_target == "node2":
        return _node_mapping()["node2"]
    confirmations = state.get("confirmations") or {}
    if confirmations.get("c2"):
        return "node3_prompt_generation"
    return "c2_select_scheme"


def _route_c3_decision(state: dict[str, Any]) -> str:
    """C3 interrupt resume 后的路由：
      - refine_target=node2 → node2_refine_schemes（改方案，refine 完会回流到 C2）
      - refine_target=node3 → node3_refine_prompts（改当前提示词，refine 完回流到 C3）
      - confirmations.c3=True → Node4
      - 其他 → 留在 C3 让用户重选
    """
    refine_target = state.get("_refine_target")
    if refine_target in ("node2", "node3"):
        return _node_mapping()[refine_target]
    confirmations = state.get("confirmations") or {}
    if confirmations.get("c3"):
        return "node4_generate_image"
    return "c3_confirm_prompt"


def _route_c4_decision(state: dict[str, Any]) -> str:
    """C4 interrupt resume 后的路由：
      - refine_target=node2 → node2_refine_schemes
      - refine_target=node3 → node3_refine_prompts
      - redo_target=node4  → Node4 完全重置 work_items（唯一保留的完全重做）
      - confirmations.c4=True → finalize
      - 其他（redo/refine 被拒，所有控制字段全清）→ 留在 C4 让用户重选
    """
    refine_target = state.get("_refine_target")
    if refine_target in ("node2", "node3"):
        return _node_mapping()[refine_target]
    redo_target = state.get("_redo_target")
    if redo_target == "node4":
        return _node_mapping()["node4"]
    confirmations = state.get("confirmations") or {}
    if confirmations.get("c4"):
        return "finalize"
    return "c4_review_result"
