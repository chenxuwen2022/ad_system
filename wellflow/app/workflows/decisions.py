"""Decision lifecycle: each revision invalidates downstream confirmations."""
from functools import wraps
from wellflow.app.workflows.state import cleared
from wellflow.app.workflow_status import revision


def request_interrupt(state, payload):
    from langgraph.types import interrupt
    payload = {**payload, "revision": revision(state, payload["node"])}
    result = interrupt(payload)
    if not isinstance(result, dict):
        return None
    if result.get("revision") and result["revision"] != payload["revision"]:
        return None
    if result.get("decision", "confirm") not in ("confirm", "refine", "redo"):
        return None
    return result


def invalidate(state, stage):
    update = {"confirmations": {f"c{i}": False for i in range(stage, 5)},
              "workflow_revision": state.get("workflow_revision", 0) + 1,
              "_redo_target": None, "_redo_instruction": None, "_refine_target": None,
              "_refine_instruction": None, "_refine_selected_indices": None, "_refine_scheme_count": None, "_refine_scheme_source": None}
    if stage < 2:
        update["node2"] = cleared()
    if stage < 3:
        old = state.get("node3") or {}
        update["node3"] = cleared(**{k: old[k] for k in ("reference_images", "ratio", "image_model") if k in old})
    if stage < 4:
        update["node4"] = cleared()
    return update


def decision_node(stage):
    def decorate(fn):
        @wraps(fn)
        def run(state):
            result = fn(state)  # GraphInterrupt must propagate without mutation.
            target = result.get("_refine_target") or result.get("_redo_target")
            target_stage = int(target[-1]) if target else stage
            update = invalidate(state, target_stage)
            # A rejected decision must not clear current products; keep their
            # downstream snapshots until an actual confirm/refine/redo is accepted.
            accepted = target or result.get("_redo_target") or (result.get("confirmations") or {}).get(f"c{stage}")
            if not accepted:
                return {**result, "_redo_target": None, "_redo_instruction": None,
                        "_refine_target": None, "_refine_instruction": None,
                        "_refine_selected_indices": None, "_refine_scheme_count": None, "_refine_scheme_source": None}
            update["confirmations"].update(result.get("confirmations") or {})
            confirmations = update["confirmations"]
            update.update(result)
            update["confirmations"] = confirmations
            return update
        return run
    return decorate


GENERATION_NODES = {
    "node1": "node1_product_analyzer", "node2": "node2_planning_scheme",
    "node3": "node3_prompt_generation", "node4": "node4_generate_image",
}
REDO_TARGETS = {
    "c1": {"node1"}, "c2": {"node2"},
    "c3": {"node2", "node3"}, "c4": {"node2", "node3", "node4"},
}


def validate_redo(state, current_node, target):
    if target not in REDO_TARGETS.get(current_node, set()):
        raise ValueError("当前阶段不能重做该节点，请先完成上游确认")
    if target == "node1" and (state.get("node1") or {}).get("report_locked"):
        raise ValueError("商品报告已锁定，请新建任务")
    if target == "node2" and not (state.get("node1") or {}).get("report_locked"):
        raise ValueError("请先确认商品报告")
    if target == "node3":
        node2 = state.get("node2") or {}
        selected = node2.get("selected_scheme_indices") or []
        counts = node2.get("per_scheme_count") or []
        schemes = node2.get("schemes") or []
        if (not selected or len(counts) != len(selected)
                or any(type(i) is not int or not 0 <= i < len(schemes) for i in selected)
                or any(type(n) is not int or n < 1 for n in counts)):
            raise ValueError("请先选择商拍方案并确认提示词数量")
    if target == "node4" and not (state.get("node3") or {}).get("generate_prompts"):
        raise ValueError("请先确认生图提示词")


def redo_decision(state, current_node, values):
    target = values.get("redo_target", "node" + current_node[-1])
    try:
        validate_redo(state, current_node, target)
    except ValueError:
        return {}  # Rejected decisions stay at the same interrupt.
    stage = int(target[-1])
    update = invalidate(state, stage)
    keep = {}
    if target == "node3":
        keep = {k: v for k, v in (state.get(target) or {}).items()
                if k in ("reference_images", "ratio", "image_model")}
    if target == "node4":
        from wellflow.app.workflows.node4_graph import prepare_image_redo
        update[target] = prepare_image_redo(state.get(target) or {})
    else:
        update[target] = cleared(**keep)
    # Remove history only for regenerated products and their dependents.
    from wellflow.app.workflows.state import _normalize_refine_history
    history = _normalize_refine_history(state.get("_refine_history"))
    update["_refine_history"] = cleared(**{k: v for k, v in history.items()
                                         if k in GENERATION_NODES and int(k[-1]) < stage})
    update.update(_redo_target=target, _redo_instruction=values.get("redo_instruction") or "")
    return update
