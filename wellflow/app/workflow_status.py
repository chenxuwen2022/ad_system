"""Canonical checkpoint view shared by reads, resumes and persistence."""
from __future__ import annotations

import hashlib
import json
from typing import Any

PHASES = {"c1": "c1_confirm", "c2": "c2_select", "c3": "c3_confirm", "c4": "c4_review"}
WAIT_NODES = {"c1_confirm_report": "c1", "c2_select_scheme": "c2",
              "c3_confirm_prompt": "c3", "c4_review_result": "c4"}
EXEC_PHASES = {"node1_product_analyzer": "node1_vlm_analyzing",
               "node2_planning_scheme": "node2_plan_scheme",
               "node3_prompt_generation": "node3_prompt_gen",
               "node4_generate_image": "node4_generation",
               "node1_refine_report": "node1_refining",
               "node2_refine_schemes": "node2_refining",
               "node3_refine_prompts": "node3_refining", "finalize": "processing"}


def canonical_phase(phase: str) -> str:
    return {"c2_confirm": "c2_select", "c4_confirm": "c4_review"}.get(phase, phase)


def canonical_interrupt(payload: dict) -> dict:
    result = dict(payload)
    node = result.get("node")
    if node in PHASES:
        result["phase"] = PHASES[node]
    return result


def revision(state: dict, node: str) -> str:
    # Bind decisions to content and the current run, not just the C1-C4 name.
    content = {"node": node, "run": state.get("workflow_revision", 0),
               "data": state.get(f"node{node[-1]}", {})}
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()[:24]


def checkpoint_view(snapshot: Any) -> tuple[str, dict | None]:
    state = getattr(snapshot, "values", None) or {}
    if not state:
        return "missing", None
    # Only a recorded interrupt proves that an input is awaited; next alone may
    # be a checkpoint written just before entering the interrupt function.
    for task in getattr(snapshot, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            value = getattr(intr, "value", None)
            if isinstance(value, dict) and value.get("node") in PHASES:
                payload = canonical_interrupt(value)
                payload.setdefault("revision", revision(state, payload["node"]))
                return payload["phase"], payload
    next_nodes = getattr(snapshot, "next", ()) or ()
    if not next_nodes:
        return ("done" if state.get("phase") == "done" else "needs_retry"), None
    for name in next_nodes:
        if name in EXEC_PHASES:
            return EXEC_PHASES[name], None
    return "processing", None
