"""父图：WellFlow 商拍任务流水线 — 真实拓扑。

子图概览：
  Node1 (子图)  — do_analyze (单节点): 输入校验 → VLM 多模态流式识别 → Markdown 报告
  Node2 (子图)  — planning_scheme (单节点): VLM 一次产出 N 套 12 维商拍方案 JSON
  Node3 (子图)  — prompt_generation (单节点): 按选中方案顺序循环调 VLM → N 条最终生图 prompt
  Node4 (子图)  — prepare → run_generation → archive (3 节点): 并发调 LLM 生图 + 归档

主干拓扑（redo 受阶段约束）：
  START ──→ Node1 ──→ C1 ──→ Node2 ──→ C2 ──→ Node3 ──→ C3 ──→ Node4 ──┐
                                                     ↑ C2 redo           │
                                              (只能回 Node2)             │
                                                                         _route_after_node4
                                                                         (永远返回 c4_review)
                                                                              ↓
                                                                          C4_review_result
                                                                              │
                                                    ┌──────────────────────────┴─────────────┐
                                                    ↓                                         ↓
                                                 confirm                                  redo:2~4
                                                    │                                         │
                                                    ↓                                  回到对应 Node
                                                 finalize                            （分层清理 state）
                                                    │
                                                    ↓
                                                   END
                                                 (done 后禁止任何 redo)

HITL interrupt 数据流：
  C1  resume:   confirmed_report, model_images(file), ratio, image_model
  C2  resume:   selected_scheme_indices + decision("confirm"|"redo") + redo_target("node2")
  C3  resume:   edited_prompts[], per_prompt_count[], per_prompt_size[], ratio, image_model
                + decision("confirm"|"redo") + redo_target("node2"~"node3")
  C4  resume:   decision("confirm"|"redo"), redo_target("node2"~"node4")

State 分层（TaskState）：
  request（不变） ─ node1 ─ node2 ─ node3 ─ node4 ─ progress/cost/interrupt/error

redo 分层清理（C2 / C3 / C4 统一逻辑）：
  【强制约束】node1 仅在 C1 阶段允许重做；一旦推进到 Node2，node1 永久锁定。
  C2 仅可 redo→node2；C3 可 redo→node2/node3；C4 可 redo→node2/node3/node4。
  finalize（phase=done，确认入库）后禁止任何 redo。
  redo→node2: 清 node2+3+4，保留 node1 报告 + node3.model_images/ratio/image_model
  redo→node3: 清 node3+4，保留 node1 + node2.schemes + node3.model_images/ratio/image_model
  redo→node4: 清 node4.outputs/failed_items，重置 work_items 为 pending（仅 C4 可达）

Phase 枚举（前端 PHASE_LABELS 对齐）：
  input → node1_input_check → node1_vlm_analyzing → node1_vlm_done
  → c1_confirm
  → node2_plan_scheme
  → c2_select
  → node3_prompt_gen
  → c3_confirm
  → node4_prepare → node4_generation → node4_archive
  → c4_review
  → done / failed

LangGraph recursion_limit=~100，支持多轮 redo 循环。
"""

from __future__ import annotations

from typing import Any


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

    from wellflow.app.workflows.state import TaskState, cleared
    from wellflow.app.workflows import (
        node1_graph as _n1,
        node2_graph as _n2,
        node3_graph as _n3,
        node4_graph as _n4,
        finalize as _finalize,
    )

    graph = StateGraph(TaskState)

    # ---- 子图 ----
    graph.add_node("node1_product_analyzer", _n1.build_graph())
    graph.add_node("node2_planning_scheme", _n2.build_graph())
    graph.add_node("node3_prompt_generation", _n3.build_graph())
    graph.add_node("node4_generate_image", _n4.build_graph())

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

    # ---- C1 条件路由：confirm → Node2，redo → Node1 ----
    graph.add_conditional_edges(
        "c1_confirm_report",
        _route_c1_decision,
        {
            "node1_product_analyzer": "node1_product_analyzer",
            "node2_planning_scheme": "node2_planning_scheme",
        },
    )

    graph.add_edge("node2_planning_scheme", "c2_select_scheme")

    # ---- C2 条件路由：confirm → Node3，redo → Node2 ----
    # 注意：C2 在 Node2 之后、Node3 之前，按约束只能重做 node2；
    # node1 在 C1 确认后永久锁定，不提供回退路径。
    graph.add_conditional_edges(
        "c2_select_scheme",
        _route_c2_decision,
        {
            "node2_planning_scheme": "node2_planning_scheme",
            "node3_prompt_generation": "node3_prompt_generation",
        },
    )

    graph.add_edge("node3_prompt_generation", "c3_confirm_prompt")

    # ---- C3 条件路由：confirm → Node4，redo → Node2/3 ----
    graph.add_conditional_edges(
        "c3_confirm_prompt",
        _route_c3_decision,
        {
            "node2_planning_scheme": "node2_planning_scheme",
            "node3_prompt_generation": "node3_prompt_generation",
            "node4_generate_image": "node4_generate_image",
        },
    )

    # ---- C4 条件路由：redo 可回 Node2/3/4，confirm 进 finalize ----
    graph.add_conditional_edges(
        "node4_generate_image",
        _route_after_node4,
        {
            "c4_review": "c4_review_result",
            "finalize": "finalize",
        },
    )
    graph.add_conditional_edges(
        "c4_review_result",
        _route_c4_decision,
        {
            "node2_planning_scheme": "node2_planning_scheme",
            "node3_prompt_generation": "node3_prompt_generation",
            "node4_generate_image": "node4_generate_image",
            "finalize": "finalize",
        },
    )

    graph.add_edge("finalize", END)

    # recursion_limit：支持深度 redo 循环（8 nodes/轮 × ~12 轮 = ~100 步）
    return graph.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# HITL interrupt 节点
# ---------------------------------------------------------------------------


def _c1_confirm_report(state: dict[str, Any]) -> dict[str, Any]:
    """C1：用户确认商品识别报告。

    interrupt 让前端展示 Node1 的 product_insight（Markdown 报告），
    用户可以修改报告内容、上传模特图、选画面比例。
    也支持 decision="redo" + redo_target="node1" 重跑 Node1。

    resume 写入：
      node1.product_insight（可能被修改）
      node3.model_images（用户上传的模特图路径）
      node3.ratio（用户选的画面比例）
    """
    from langgraph.types import interrupt
    from wellflow.app.event_bus import publish

    task_id = state.get("task_id", "")

    if task_id:
        publish(task_id, "phase", {"phase": "c1_confirm"})

    interrupt_value = interrupt({
        "node": "c1",
        "phase": "c1_confirm",
        "hint": "请确认商品识别报告，可修改后继续",
        "report": state.get("node1", {}).get("product_insight", ""),
        # 结构化四块（稳定 key），前端重点洞察面板优先消费，避免耦合 prompt 字段名
        "report_sections": state.get("node1", {}).get("report_sections"),
    })

    if not interrupt_value:
        return {"phase": "c1_confirm"}

    decision = interrupt_value.get("decision", "confirm")
    redo_target = interrupt_value.get("redo_target", "node1")

    # ---- redo：只支持 redo→node1（C1 刚跑完 Node1，没有其他上游）----
    if decision == "redo":
        print(f"[c1_confirm_report] 🔄 redo → {redo_target}", flush=True)
        # 清 node1-4（cleared() 整体替换，防止 reducer 字段级 merge 残留旧数据），重跑 VLM
        new_state: dict[str, Any] = {
            "phase": "c1_confirm",
            "node1": cleared(),
            "node2": cleared(),
            "node3": cleared(),
            "node4": cleared(),
            "_redo_target": "node1",
        }
        return new_state

    # ---- confirm：正常处理 ----
    node1 = state.get("node1", {})
    new_node1 = dict(node1)
    new_node3 = dict(state.get("node3", {}))

    confirmed_report = interrupt_value.get("confirmed_report")
    if confirmed_report:
        new_node1["product_insight"] = confirmed_report
        # 用户可能编辑过报告，四块结构按编辑后文本重新归一化
        from wellflow.app.prompt.report_sections import build_report_sections
        new_node1["report_sections"] = build_report_sections(confirmed_report)

    model_images = interrupt_value.get("model_images")
    if model_images:
        new_node3["model_images"] = model_images

    ratio = interrupt_value.get("ratio")
    if ratio:
        new_node3["ratio"] = ratio

    image_model = interrupt_value.get("image_model")
    if image_model:
        new_node3["image_model"] = image_model

    return {"phase": "c1_confirm", "node1": new_node1, "node3": new_node3, "_redo_target": None}


def _c2_select_scheme(state: dict[str, Any]) -> dict[str, Any]:
    """C2：用户从 3 套方案中选 1-3 套。

    interrupt 让前端展示 Node2 的 schemes（3 套 12 维 JSON），
    用户可以选方案（selected_scheme_indices）或选择 redo 回到上游。

    resume 写入 node2.selected_scheme_indices。
    也支持 decision("confirm"|"redo") + redo_target("node2") 做 redo（仅允许 node2）。
    """
    from langgraph.types import interrupt
    from wellflow.app.event_bus import publish

    task_id = state.get("task_id", "")
    node1 = state.get("node1", {})
    node2 = state.get("node2", {})
    node3 = state.get("node3", {})

    if task_id:
        publish(task_id, "phase", {"phase": "c2_select"})

    interrupt_value = interrupt({
        "node": "c2",
        "phase": "c2_select",
        "hint": "从 3 套商拍方案中选择 1-3 套继续生成生图提示词",
        "schemes": node2.get("schemes", []),
        "scheme_raw": node2.get("scheme_raw", ""),
        "model_images": node3.get("model_images", []),
    })

    if not interrupt_value:
        return {"phase": "c2_select"}

    decision = interrupt_value.get("decision", "confirm")
    redo_target = interrupt_value.get("redo_target", "node2")

    # ---- redo：C2 阶段仅允许重做 node2（node1 已永久锁定）----
    if decision == "redo":
        print(f"[c2_select] 🔄 redo → {redo_target}", flush=True)

        # 无论请求目标是什么，C2 只允许回 Node2：整体替换清 node2-4，保留 node1 报告
        # 以及 node3 里 C1 上传的模特图/比例/模型选择（用户已传资产不丢）
        print("  → 清 node2-4，保留 node1 报告，回 Node2", flush=True)
        new_state: dict[str, Any] = {
            "phase": "c2_select",
            "node1": node1,
            "node2": cleared(),
            "node3": cleared(
                model_images=node3.get("model_images", []),
                ratio=node3.get("ratio"),
                image_model=node3.get("image_model"),
            ),
            "node4": cleared(),
            "_redo_target": "node2",
        }
        return new_state

    # ---- confirm：正常处理 selected_scheme_indices ----
    selected = interrupt_value.get("selected_scheme_indices")
    new_node2 = dict(node2)
    if selected is not None:
        new_node2["selected_scheme_indices"] = list(selected)
    else:
        # 没选就默认全选
        new_node2["selected_scheme_indices"] = list(range(len(node2.get("schemes", []))))

    # 前端可能覆盖 ratio / image_model / model_images
    new_node3 = dict(state.get("node3", {}))
    ratio = interrupt_value.get("ratio")
    if ratio:
        new_node3["ratio"] = ratio
    image_model = interrupt_value.get("image_model")
    if image_model:
        new_node3["image_model"] = image_model
    model_images = interrupt_value.get("model_images")
    if model_images:
        new_node3["model_images"] = model_images

    return {"phase": "c2_select", "node2": new_node2, "node3": new_node3, "_redo_target": None}


def _c3_confirm_prompt(state: dict[str, Any]) -> dict[str, Any]:
    """C3：用户确认每套 prompt 的最终内容 + 选张数 + 选规格。

    interrupt 让前端展示 Node3 的 generate_prompts + prompts_detail，
    用户可以：
      - 编辑每套 prompt（edited_prompts）
      - 为每套选生成张数（per_prompt_count: [3, 1, ...]）
      - 为每套选图片规格（per_prompt_size: ["3:4", "3:4", ...]）
      - redo 回到上游 Node2/Node3（node1 已锁定，node4 尚未执行）

    resume 写入 node3.generate_prompts（可能被编辑）+ per_prompt_count + per_prompt_size。
    也支持 decision("confirm"|"redo") + redo_target("node2"|"node3") 做 redo。
    """
    from langgraph.types import interrupt
    from wellflow.app.event_bus import publish

    task_id = state.get("task_id", "")
    node1 = state.get("node1", {})
    node2 = state.get("node2", {})
    node3 = state.get("node3", {})

    if task_id:
        publish(task_id, "phase", {"phase": "c3_confirm"})

    interrupt_value = interrupt({
        "node": "c3",
        "phase": "c3_confirm",
        "hint": "请确认每套方案的最终提示词，可编辑后继续生图",
        "generate_prompts": node3.get("generate_prompts", []),
        "prompts_detail": node3.get("prompts_detail", []),
        "model_images": node3.get("model_images", []),
    })

    if not interrupt_value:
        return {"phase": "c3_confirm"}

    decision = interrupt_value.get("decision", "confirm")
    redo_target = interrupt_value.get("redo_target", "node3")

    # ---- redo：C3 阶段仅允许重做 node2/node3（node1 已锁定，node4 未执行）----
    if decision == "redo":
        print(f"[c3_confirm] 🔄 redo → {redo_target}", flush=True)
        new_state: dict[str, Any] = {"phase": "c3_confirm"}

        if redo_target == "node2":
            print("  → 清 node2-4，保留 node1 报告，回 Node2", flush=True)
            new_state["node1"] = node1
            new_state["node2"] = cleared()
            new_state["node3"] = cleared(
                model_images=node3.get("model_images", []),
                ratio=node3.get("ratio"),
                image_model=node3.get("image_model"),
            )
            new_state["node4"] = cleared()
            new_state["_redo_target"] = "node2"
        else:
            # 默认回 Node3：清 node3-4，保留 node1+node2
            print("  → 清 node3-4，保留 node1+node2，回 Node3", flush=True)
            new_state["node1"] = node1
            new_state["node2"] = node2
            new_state["node3"] = cleared(
                model_images=node3.get("model_images", []),
                ratio=node3.get("ratio"),
                image_model=node3.get("image_model"),
            )
            new_state["node4"] = cleared()
            new_state["_redo_target"] = "node3"

        return new_state

    # ---- confirm：正常处理 prompt 编辑 + 张数 + 规格 + 选中过滤 ----
    new_node3 = dict(node3)

    # prompt 编辑覆盖
    edited = interrupt_value.get("edited_prompts")
    if edited and isinstance(edited, list):
        new_node3["generate_prompts"] = list(edited)

    # 每 prompt 张数
    per_count = interrupt_value.get("per_prompt_count")
    if per_count and isinstance(per_count, list):
        new_node3["per_prompt_count"] = [int(n) for n in per_count]

    # 每 prompt 规格
    per_size = interrupt_value.get("per_prompt_size")
    if per_size and isinstance(per_size, list):
        new_node3["per_prompt_size"] = list(per_size)

    # 兜底：没有 count/size 默认每张 1 张、默认规格
    prompts = new_node3.get("generate_prompts", [])
    if not new_node3.get("per_prompt_count"):
        new_node3["per_prompt_count"] = [1] * len(prompts)
    if not new_node3.get("per_prompt_size"):
        new_node3["per_prompt_size"] = ["3:4"] * len(prompts)

    # ---- selected_prompt_indices：前端 checkbox 选中哪些 prompt 才传给 Node4 ----
    selected_indices = interrupt_value.get("selected_prompt_indices")
    if selected_indices and isinstance(selected_indices, list):
        sel_set = set(int(i) for i in selected_indices)
        orig_len = len(new_node3.get("generate_prompts", []))
        sel_sorted = sorted(i for i in sel_set if 0 <= i < orig_len)
        print(f"[c3_confirm] ☑️ selected_prompt_indices={sel_sorted} (原始 {orig_len} 条)", flush=True)

        if sel_sorted:
            new_node3["generate_prompts"] = [
                new_node3["generate_prompts"][i] for i in sel_sorted
            ]
            if new_node3.get("prompts_detail"):
                new_node3["prompts_detail"] = [
                    new_node3["prompts_detail"][i] for i in sel_sorted
                ]
            if new_node3.get("per_prompt_count"):
                new_node3["per_prompt_count"] = [
                    new_node3["per_prompt_count"][i] for i in sel_sorted
                ]
            if new_node3.get("per_prompt_size"):
                new_node3["per_prompt_size"] = [
                    new_node3["per_prompt_size"][i] for i in sel_sorted
                ]
            print(f"[c3_confirm] ✂️ 过滤后剩 {len(sel_sorted)} 条 prompt → Node4", flush=True)
        else:
            print("[c3_confirm] ⚠️ selected_prompt_indices 为空，无 prompt 选中！", flush=True)

    # ratio / image_model / model_images 覆盖
    ratio = interrupt_value.get("ratio")
    if ratio:
        new_node3["ratio"] = ratio
    image_model = interrupt_value.get("image_model")
    if image_model:
        new_node3["image_model"] = image_model
    model_images = interrupt_value.get("model_images")
    if model_images:
        new_node3["model_images"] = model_images

    return {"phase": "c3_confirm", "node3": new_node3, "_redo_target": None}


def _c4_review_result(state: dict[str, Any]) -> dict[str, Any]:
    """C4：用户查看生图结果，选择 confirm 或 redo 到 Node2/3/4（node1 已锁定）。

    interrupt 让前端展示 Node4 的 outputs（生图结果）+ failed_items（失败明细），
    用户可以：
      - confirm：进 finalize 归档（入库后禁止任何 redo）
      - redo_to='node2'：清 node2+node3+node4（保留 node1 报告 + node3.model_images/ratio）
      - redo_to='node3'：清 node3+node4（保留 node1+node2 方案）
      - redo_to='node4'：只重置 node4.work_items status + 清 outputs（原来的 redo）
    按约束 node1 已永久锁定，不提供 redo→node1。
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

    interrupt_value = interrupt({
        "node": "c4",
        "phase": "c4_review",
        "hint": "查看生图结果，可选择重做或确认归档",
        "outputs": node4.get("outputs", []),
        "failed_items": node4.get("failed_items", []),
        "reference_images": req.get("product_images", []),
        "model_images": node3.get("model_images", []),
        "generate_prompts": node3.get("generate_prompts", []),
    })

    if not interrupt_value:
        return {"phase": "c4_review"}

    decision = interrupt_value.get("decision", "confirm")  # "confirm" or "redo"
    redo_target = interrupt_value.get("redo_target", "node4")  # "node2" / "node3" / "node4"

    if decision == "confirm":
        return {"phase": "c4_review", "_redo_target": None}

    # ---- redo：按 target 分层清理 state（node1 已锁定，仅允许 node2/3/4）----
    new_state: dict[str, Any] = {"phase": "c4_review"}

    if redo_target == "node2":
        # 整体替换清 node2+node3+node4，保留 node1 报告
        print(f"[c4_review] 🔄 redo → Node2（清 node2-4，保留 node1）", flush=True)
        new_state["node1"] = node1  # 保留报告
        new_state["node2"] = cleared()
        # node3 清方案产物，但保留 C1 上传的模特图/比例/模型选择
        new_state["node3"] = cleared(
            model_images=node3.get("model_images", []),
            ratio=node3.get("ratio"),
            image_model=node3.get("image_model"),
        )
        new_state["node4"] = cleared()
        new_state["_redo_target"] = "node2"

    elif redo_target == "node3":
        # 清 node3+node4，保留 node1+node2
        print(f"[c4_review] 🔄 redo → Node3（清 node3-4，保留 node1-2）", flush=True)
        new_state["node1"] = node1
        new_state["node2"] = node2
        new_state["node3"] = cleared(
            model_images=node3.get("model_images", []),
            ratio=node3.get("ratio"),
            image_model=node3.get("image_model"),
        )
        new_state["node4"] = cleared()
        new_state["_redo_target"] = "node3"

    else:
        # redo_target == "node4" 或其他非法值：只重置 work_items status + 清 outputs（原来的行为）
        print(f"[c4_review] 🔄 redo → Node4（只重置 work_items）", flush=True)
        new_state["node1"] = node1
        new_state["node2"] = node2
        new_state["node3"] = node3
        new_node4 = dict(node4)
        items = new_node4.get("work_items", [])
        for it in items:
            it["status"] = "pending"
        new_node4["outputs"] = []
        new_node4["failed_items"] = []
        new_state["node4"] = new_node4
        new_state["_redo_target"] = "node4"

    # 🔑 写入 _redo_target：让 _route_c4_decision 知道该路由到哪个 Node
    return new_state


# ---------------------------------------------------------------------------
# 条件路由
# ---------------------------------------------------------------------------


def _node_mapping() -> dict[str, str]:
    """redo_target → graph node 名 的统一映射。"""
    return {
        "node1": "node1_product_analyzer",
        "node2": "node2_planning_scheme",
        "node3": "node3_prompt_generation",
        "node4": "node4_generate_image",
    }


def _route_c1_decision(state: dict[str, Any]) -> str:
    """C1 interrupt resume 后的路由：
      - confirm → Node2 (planning_scheme)
      - redo    → 只能回到 Node1（C1 在 Node1 之后，没有其他上游）
    """
    redo_target = state.get("_redo_target")
    if redo_target:
        return _node_mapping()["node1"]
    return "node2_planning_scheme"


def _route_c2_decision(state: dict[str, Any]) -> str:
    """C2 interrupt resume 后的路由：
      - confirm → Node3 (prompt_generation)
      - redo    → 只能回 Node2（node1 在 C1 确认后永久锁定）
    """
    redo_target = state.get("_redo_target")
    if redo_target:
        return "node2_planning_scheme"
    return "node3_prompt_generation"


def _route_c3_decision(state: dict[str, Any]) -> str:
    """C3 interrupt resume 后的路由：
      - confirm → Node4 (generate_image)
      - redo    → 按 state._redo_target 回到 Node2/Node3（非法值兜底回 Node3）
    """
    redo_target = state.get("_redo_target")
    if redo_target:
        safe_targets = {"node2", "node3"}
        target = redo_target if redo_target in safe_targets else "node3"
        return _node_mapping()[target]
    return "node4_generate_image"


def _route_after_node4(state: dict[str, Any]) -> str:
    """Node4（生图）完成后，路由到 C4 还是直接 finalize。

    如果有失败图 → 一定进 C4（让用户决定 redo）
    如果全部成功 → 也进 C4（让用户确认）
    （留 finalize 作为未来 skip C4 的路由）
    """
    return "c4_review"


def _route_c4_decision(state: dict[str, Any]) -> str:
    """C4 interrupt resume 后的路由：
      - confirm → finalize
      - redo    → 按 state._redo_target 回到 Node2/3/4（node1 已锁定，非法值兜底回 Node4）
    """
    redo_target = state.get("_redo_target")
    if redo_target:
        safe_targets = {"node2", "node3", "node4"}
        target = redo_target if redo_target in safe_targets else "node4"
        return _node_mapping()[target]
    return "finalize"
