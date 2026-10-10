"""Stable errors shared by the core and its adapters."""

from __future__ import annotations

from collections.abc import Mapping
import math
import re
from types import MappingProxyType


RECOVERY_REASONS = frozenset({
    "server_cooldown", "transient_network", "request_budget", "dependency_missing",
    "local_environment", "unsupported_layout", "validation_failed", "output_conflict",
    "state_invalid", "cancelled", "authentication_failed", "selection_required", "operation_failed",
})
RECOVERY_SCOPES = frozenset({"item", "task", "environment"})
RECOVERY_ACTIONS = frozenset({"retry", "defer", "derive", "inspect", "new_version", "skip"})
_OPERATIONS = frozenset({"metadata", "hls", "head", "image", "motion", "probe", "decode",
                         "state", "output", "diagnostics"})


def safe_context(value):
    """Admit only defined recovery facts, never free text, paths or responses."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("Invalid error context")
    result = {}
    for key, item in value.items():
        if key in {"reason", "scope", "operation"}:
            allowed = {"reason": RECOVERY_REASONS, "scope": RECOVERY_SCOPES, "operation": _OPERATIONS}[key]
            valid = isinstance(item, str) and item in allowed
        elif key == "retry_at":
            valid = type(item) in (int, float) and 0 <= item <= 10 ** 20 and math.isfinite(item)
        elif key == "http_status":
            valid = type(item) is int and 100 <= item <= 599
        elif key == "attempt":
            valid = type(item) is int and 1 <= item <= 3
        else:
            valid = False
        if not valid:
            raise ValueError("Invalid error context field")
        result[key] = item
    return result


class ArtworkError(Exception):
    """An actionable failure whose public fields contain no response body."""

    def __init__(
        self,
        code: str,
        message: str,
        category: str = "discovery",
        retryable: bool = False,
        *,
        context: Mapping | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = category
        self.retryable = retryable
        self.context = MappingProxyType(safe_context(context))

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"

    def to_dict(self) -> dict[str, object]:
        result = {
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.context:
            result["context"] = dict(self.context)
        return result


def recovery_for(error, *, scope="item", item_id=None):
    """Describe allowed *explicit* recovery; this never starts another attempt."""
    if scope not in RECOVERY_SCOPES:
        raise ValueError("Invalid recovery scope")
    if item_id is not None and (not isinstance(item_id, str)
                               or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item_id) is None):
        raise ValueError("Invalid recovery item identity")
    if isinstance(error, ArtworkError):
        code, category, context = error.code, error.category, safe_context(error.context)
    elif isinstance(error, Mapping):
        code, category, context = error.get("code"), error.get("category"), safe_context(error.get("context"))
    else:
        raise ValueError("Invalid recovery error")
    if code == "RETRY_AFTER_DEADLINE":
        reason, actions = "server_cooldown", ["retry", "defer"]
    elif code in {"REQUEST_LIMIT", "RUNTIME_BUSY"}:
        reason, scope, actions = "request_budget", "task", ["retry", "defer"]
    elif code in {"NETWORK_ERROR", "NETWORK_TIMEOUT", "HTTP_TRANSIENT_ERROR"}:
        reason, actions = "transient_network", ["retry", "defer"]
    elif code in {"MOTION_DEPENDENCY_MISSING", "MEDIA_TOOL_MISSING", "MEDIA_TOOL_UNAVAILABLE"}:
        reason, scope, actions = "dependency_missing", "environment", ["retry", "defer", "inspect"]
    elif code in {"MEDIA_TOOL_TIMEOUT", "MEDIA_TOOL_OUTPUT_LIMIT", "OUTPUT_PLATFORM_UNSUPPORTED"}:
        reason, scope, actions = "local_environment", "environment", ["retry", "defer", "inspect"]
    elif isinstance(code, str) and (code.startswith("RUN_STATE_") or code == "OUTPUT_IDENTITY_CHANGED"):
        reason, scope, actions = "state_invalid", "task", ["inspect", "derive"]
    elif code in {"OUTPUT_CONFLICT", "MANIFEST_CONFLICT", "OUTPUT_BUSY"}:
        reason, scope, actions = "output_conflict", "task", ["inspect", "derive", "defer"]
    elif code == "CANCELLED":
        reason, actions = "cancelled", ["retry", "defer"]
    elif category == "auth":
        reason, scope, actions = "authentication_failed", "environment", ["retry", "defer", "inspect"]
    elif category == "validation":
        reason, actions = "validation_failed", ["inspect", "derive", "defer"]
    elif category == "rendition":
        reason, actions = "unsupported_layout", ["inspect", "derive", "defer"]
    elif category in {"input", "selection"}:
        reason, scope, actions = "selection_required", "task", ["inspect", "derive"]
    else:
        reason, actions = "operation_failed", ["inspect", "defer"]
    result = {"reason": context.get("reason", reason), "scope": context.get("scope", scope),
              "retry_at": context.get("retry_at"), "allowed_actions": actions}
    if item_id is not None:
        result["item_id"] = item_id
    return result
