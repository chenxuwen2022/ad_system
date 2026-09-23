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
              "_redo_target": None, "_refine_target": None,
              "_refine_instruction": None, "_refine_selected_indices": None}
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
            target = result.get("_refine_target")
            target_stage = int(target[-1]) if target else stage
            update = invalidate(state, target_stage)
            # A rejected decision must not clear current products; keep their
            # downstream snapshots until an actual confirm/refine/redo is accepted.
            accepted = target or result.get("_redo_target") or (result.get("confirmations") or {}).get(f"c{stage}")
            if not accepted:
                update.pop("node2", None)
                update.pop("node3", None)
                update.pop("node4", None)
            update["confirmations"].update(result.get("confirmations") or {})
            confirmations = update["confirmations"]
            update.update(result)
            update["confirmations"] = confirmations
            return update
        return run
    return decorate
