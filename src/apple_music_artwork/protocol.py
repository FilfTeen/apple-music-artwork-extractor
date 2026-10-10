"""Versioned query envelopes and explicit, plan-bound user decisions."""

from copy import deepcopy

from .errors import ArtworkError
from .run_state import fingerprint


PROTOCOL_VERSION = 1
LIFECYCLES = ("planned", "running", "needs_action", "deferred", "completed", "cancelled", "failed")
RESULT_STATUSES = ("success", "partial", "failed")
_LABELS = {"retry": "重试", "defer": "稍后处理", "skip": "跳过此项", "new_version": "获取新版",
           "derive": "按新参数创建关联任务", "inspect": "查看问题"}


def envelope(kind, data=None, *, status="success", errors=(), warnings=()):
    return {"schema_version": PROTOCOL_VERSION, "kind": kind, "status": status,
            "data": data, "errors": list(errors), "warnings": list(warnings)}


def decision(plan_id, kind, reason, item_ids, actions):
    options = [{"action": action, "label": _LABELS.get(action, action), "params": {}}
               if isinstance(action, str) else deepcopy(action) for action in actions]
    basis = {"plan_id": plan_id, "kind": kind, "reason": reason,
             "item_ids": sorted(item_ids), "options": options}
    return {**basis, "decision_id": fingerprint(basis), "default": None}


def validate_choice(context, choice):
    if (not isinstance(choice, dict) or set(choice) - {"plan_id", "decision_id", "action", "params"}
            or choice.get("plan_id") != context["plan_id"]
            or choice.get("decision_id") != context["decision_id"]):
        raise ArtworkError("DECISION_STALE", "The choice belongs to a different plan or decision; inspect the current plan.", "input")
    params = choice.get("params", {})
    option = next((value for value in context["options"]
                   if value["action"] == choice.get("action") and isinstance(params, dict)
                   and fingerprint(value.get("params", {})) == fingerprint(params)), None)
    if option is None:
        raise ArtworkError("INVALID_DECISION", "The selected action or parameters are not offered by this decision.", "input")
    return {"plan_id": choice["plan_id"], "decision_id": choice["decision_id"],
            "action": choice["action"], "params": deepcopy(params)}


def describe():
    return {"protocol_version": PROTOCOL_VERSION, "query_schema_version": 1, "download_schema_version": 2,
            "query_fields": ["schema_version", "kind", "status", "data", "errors", "warnings"],
            "comparison_fields": ["task_id", "left", "right", "outcome", "changes", "ambiguities", "limitations", "network_scope"],
            "comparison_outcomes": ["observed_change", "no_observed_change", "indeterminate"],
            "comparison_fact_fields": ["available", "present", "value"],
            "comparison_scope": "target_field_states_and_original_selected_assets_metadata_only",
            "plan_fields": ["protocol_version", "plan_id", "task_id", "target", "destination",
                            "canonical_options", "snapshot", "local_observations", "items", "decisions"],
            "control_fields": ["protocol_version", "lifecycle", "task_id", "plan_id", "decision_required", "decisions", "resume"],
            "decision_fields": ["plan_id", "decision_id", "kind", "reason", "item_ids", "options", "default"],
            "choice_fields": ["plan_id", "decision_id", "action", "params"],
            "lifecycles": list(LIFECYCLES),
            "exit_codes": {"0": "completed", "1": "internal", "2": "input", "3": "auth", "4": "api",
                           "5": "discovery_or_selection", "6": "download_or_rendition", "7": "validation",
                           "8": "saved_needs_decision", "9": "saved_deferred", "130": "cancelled"},
            "selection_binding": "plan_id_and_decision_id_and_exact_offered_action_parameters",
            "events": {"schema_version": 1, "channel": "stderr_jsonl", "snapshot": "complete_coordinator_state"}}
