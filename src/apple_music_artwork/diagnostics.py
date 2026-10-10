"""Local capability checks and bounded, coordinator-owned diagnostic records."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
import importlib
import importlib.metadata
import json
import math
import os
import re
import shutil
import sys
from threading import get_ident

from .errors import ArtworkError, RECOVERY_ACTIONS, RECOVERY_REASONS, RECOVERY_SCOPES
from .jsonio import loads_json
from .protocol import LIFECYCLES, RESULT_STATUSES


MAX_LOG_BYTES = 1024 * 1024
MAX_SUMMARY_BYTES = 64 * 1024
_KINDS = frozenset({"stage", "transfer", "retry", "cooldown", "decode", "snapshot", "plan"})
_STAGES = frozenset({"metadata", "inspect", "check_existing", "queued", "download", "waiting_validation",
                     "validate", "save", "reused", "committed", "cancelled", "failed", "skipped",
                     "needs_action", "deferred"})
_COUNTS = frozenset({"pending", "downloading", "waiting_validation", "validating", "saving", "completed",
                    "reused", "failed", "skipped", "skipped_issues", "total", "cancelled", "not_started", "new_downloaded", "semantic_references"})
_STATS = frozenset({"requests", "received_bytes", "cache_hits", "window_id", "window_requests", "max_requests", "local_reused"})
_INTEGER_FIELDS = frozenset({"schema_version", "sequence", "attempt", "ordinal", "total", "active", "queued",
                           "needs_action", "received_bytes", "expected_bytes", "total_bytes", "window_id",
                           "item_count", "decision_count"})
_NUMBER_FIELDS = frozenset({"retry_at", "retry_after_seconds", "processed_seconds", "total_seconds"})
_CATEGORIES = frozenset({"internal", "input", "auth", "api", "discovery", "selection", "rendition", "download", "validation"})
_STATUS = frozenset(RESULT_STATUSES) | frozenset({"complete", "incomplete", "pending", "downloaded", "reused",
                                               "needs_action", "deferred", "cancelled", "completed"})
_IDENTITY = re.compile(r"(?:[0-9a-f]{64}|(?:file_|issue_asset_)[0-9a-f]{64}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_LOG_NAMES = ("events-1.jsonl", "events-2.jsonl", "events-3.jsonl")


def _integer(value):
    return type(value) is int and 0 <= value <= 2 ** 63 - 1


def _number(value):
    return type(value) in (int, float) and 0 <= value <= 10 ** 20 and math.isfinite(value)


def _counts(value, allowed):
    return {key: item for key, item in value.items() if key in allowed and _integer(item)} if isinstance(value, Mapping) else {}


def _facts(value, *, item=False):
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key, data in value.items():
        if key in _INTEGER_FIELDS and _integer(data):
            result[key] = data
        elif key in _NUMBER_FIELDS and _number(data):
            result[key] = data
        elif key in {"run_id", "task_id", "plan_id", "item_id", "decision_id"}:
            if data is None or (isinstance(data, str) and _IDENTITY.fullmatch(data)):
                result[key] = data
        elif key == "kind" and isinstance(data, str) and data in ({"image", "video"} if item else _KINDS):
            result[key] = data
        elif key == "stage" and isinstance(data, str) and data in _STAGES:
            result[key] = data
        elif key == "lifecycle" and isinstance(data, str) and data in LIFECYCLES:
            result[key] = data
        elif key == "reason" and isinstance(data, str) and data in RECOVERY_REASONS:
            result[key] = data
        elif key == "scope" and isinstance(data, str) and data in RECOVERY_SCOPES:
            result[key] = data
        elif key == "action" and isinstance(data, str) and data in RECOVERY_ACTIONS | {"download", "reuse", "reused", "skip"}:
            result[key] = data
        elif key == "status" and isinstance(data, str) and data in _STATUS:
            result[key] = data
        elif key == "code" and isinstance(data, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", data):
            result[key] = data
        elif key == "category" and isinstance(data, str) and data in _CATEGORIES:
            result[key] = data
        elif key in {"verified", "retryable"} and type(data) is bool:
            result[key] = data
        elif key == "allowed_actions" and isinstance(data, (list, tuple)):
            result[key] = [entry for entry in data[:8] if isinstance(entry, str) and entry in RECOVERY_ACTIONS]
    return result


def event_record(event):
    """Project one already sequenced event; never retain arbitrary nested data."""
    clean = _facts(event)
    if isinstance(event, Mapping):
        for key, allowed in (("counts", _COUNTS), ("stats", _STATS)):
            if key in event:
                clean[key] = _counts(event[key], allowed)
        if isinstance(event.get("items"), (list, tuple)):
            clean["items"] = [_facts(item, item=True) for item in event["items"][:4] if isinstance(item, Mapping)]
        if event.get("kind") == "plan" and isinstance(event.get("plan"), Mapping):
            plan = event["plan"]
            clean.update({key: value for key, value in _facts(plan).items() if key in {"plan_id", "task_id"}})
            for key, count in (("items", "item_count"), ("decisions", "decision_count")):
                if isinstance(plan.get(key), (list, tuple)):
                    clean[count] = len(plan[key])
    return clean


def _issue_record(value):
    clean = _facts(value)
    if not isinstance(value, Mapping):
        return clean
    for key in ("error", "recovery", "context"):
        if isinstance(value.get(key), Mapping):
            clean.update(_facts(value[key]))
    reason = value.get("reason")
    if isinstance(reason, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", reason):
        clean.setdefault("code", reason)
    if isinstance(value.get("options"), (list, tuple)):
        clean["allowed_actions"] = [option["action"] for option in value["options"][:8]
                                    if isinstance(option, Mapping) and isinstance(option.get("action"), str)
                                    and option["action"] in RECOVERY_ACTIONS]
    if isinstance(value.get("item_ids"), (list, tuple)):
        clean["item_ids"] = [item for item in value["item_ids"] if isinstance(item, str) and _IDENTITY.fullmatch(item)]
    return clean


def _version(value):
    return value if isinstance(value, str) and len(value) <= 80 and re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}[-+._A-Za-z0-9]{0,64}", value) else "unrecognized"


def doctor(*, environ=None, runner=None):
    """Check local prerequisites only; locating tools never proves decoding."""
    from .hls import require_parser
    from .motion import MediaTools

    environ = os.environ if environ is None else environ
    checks = [{"id": "python", "status": "ok" if sys.version_info >= (3, 11) else "unsupported",
               "version": ".".join(str(part) for part in sys.version_info[:3])}]
    required = (os.open, os.mkdir, os.stat, os.link, os.unlink)
    platform_ready = (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
                      and all(operation in os.supports_dir_fd for operation in required))
    checks.append({"id": "output_platform", "status": "ok" if platform_ready else "unsupported"})
    try:
        pillow = importlib.import_module("PIL.Image")
        pillow_ready = callable(getattr(pillow, "open", None))
        checks.append({"id": "pillow", "status": "ok" if pillow_ready else "unsupported",
                       "version": _version(importlib.metadata.version("Pillow"))})
    except (ImportError, importlib.metadata.PackageNotFoundError):
        pillow_ready = False
        checks.append({"id": "pillow", "status": "missing", "code": "IMAGE_DEPENDENCY_MISSING"})
    try:
        require_parser()
        parser_ready = True
        checks.append({"id": "motion_parser", "status": "ok"})
    except ArtworkError:
        parser_ready = False
        checks.append({"id": "motion_parser", "status": "missing", "code": "MOTION_DEPENDENCY_MISSING"})
    try:
        checks.append({"id": "terminal_ui", "status": "ok", "version": _version(importlib.metadata.version("rich"))})
    except importlib.metadata.PackageNotFoundError:
        checks.append({"id": "terminal_ui", "status": "missing"})
    candidates = {name: environ.get("APPLE_ARTWORK_" + name.upper()) or shutil.which(name, path=environ.get("PATH", ""))
                  for name in ("ffmpeg", "ffprobe")}
    tools_ready = False
    if not all(candidates.values()):
        checks.append({"id": "media_tools", "status": "missing", "code": "MEDIA_TOOL_MISSING"})
    else:
        try:
            tools = MediaTools.discover(**candidates, runner=runner)
            tools_ready = True
            checks.append({"id": "media_tools", "status": "ok",
                           "versions": {"ffmpeg": _version(tools.ffmpeg_version), "ffprobe": _version(tools.ffprobe_version)}})
        except ArtworkError as error:
            checks.append({"id": "media_tools", "status": "failed", "code": error.code})
    return {"network_used": False, "checks": checks,
            "capabilities": {"static_download": platform_ready and pillow_ready,
                             "motion_inspection": parser_ready,
                             "motion_download": platform_ready and parser_ready and tools_ready},
            "verification": {"media_decode": "not_run"}}


def _json(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")


def _invalid():
    return ArtworkError("DIAGNOSTIC_STATE_INVALID", "Diagnostic ownership or recovery records could not be verified.", "internal")


class EventJournal:
    """Three bounded files under a task lock, with a small ownership ledger.

    The caller owns the open directory and task lock. Workers never call this
    object. A recorded replacement is recovered only from its exact old and
    new identities; unregistered names are not cleanup or overwrite authority.
    Diagnostic failures disable this journal and produce separate warnings.
    """

    def __init__(self, directory, *, task_id, read_only=False, max_file_bytes=MAX_LOG_BYTES):
        if not isinstance(task_id, str) or not _DIGEST.fullmatch(task_id):
            raise ValueError("Invalid diagnostic task identity")
        if type(max_file_bytes) is not int or not 256 <= max_file_bytes <= MAX_LOG_BYTES:
            raise ValueError("Invalid diagnostic file limit")
        self.directory, self.task_id, self.read_only = directory, task_id, read_only
        self.max_file_bytes, self.warnings = max_file_bytes, []
        self.enabled, self._thread = True, get_ident()
        self._buffer, self._buffer_bytes = [], 0
        self._ledger_record = None
        self._ledger = {"schema_version": 1, "task_id": task_id, "logs": [], "summary": None, "pending": None}
        try:
            raw = self._read_optional("journal.json", MAX_SUMMARY_BYTES)
            if raw is None:
                if directory.names():
                    raise _invalid()
                if not read_only:
                    self._store_ledger()
            else:
                body, self._ledger_record = raw
                self._ledger = loads_json(body)
                self._validate_ledger()
                if self._ledger["pending"] is not None:
                    if read_only:
                        self._warn("DIAGNOSTIC_PENDING", disable=False)
                    else:
                        self._recover_pending()
                if self._ledger["pending"] is None:
                    for record in self._ledger["logs"]:
                        self._read_registered(record, MAX_LOG_BYTES)
                    if self._ledger["summary"] is not None:
                        self._read_registered(self._ledger["summary"], MAX_SUMMARY_BYTES)
        except Exception:
            self._warn("DIAGNOSTIC_STATE_INVALID")

    def _warn(self, code, *, disable=True):
        if not any(item["code"] == code for item in self.warnings):
            self.warnings.append({"code": code, "category": "internal",
                                  "message": "Diagnostic records are incomplete or unavailable; task state is reported separately.",
                                  "retryable": False})
        if disable:
            self.enabled = False
            self._buffer, self._buffer_bytes = [], 0

    def _writable(self):
        if self.read_only:
            raise RuntimeError("A read-only journal cannot write")
        if get_ident() != self._thread:
            self._warn("DIAGNOSTIC_WRITER_CHANGED")
        return self.enabled

    def _read_optional(self, name, limit):
        try:
            body, identity = self.directory.read_file(name, max_bytes=limit)
        except FileNotFoundError:
            return None
        return body, {"name": name, **identity, "sha256": sha256(body).hexdigest()}

    def _valid_record(self, record, *, names, limit):
        return (isinstance(record, dict) and set(record) == {"name", "device", "inode", "size", "mtime_ns", "sha256"}
                and record["name"] in names and all(_integer(record.get(key)) for key in ("device", "inode", "size", "mtime_ns"))
                and record["size"] <= limit and isinstance(record.get("sha256"), str)
                and _DIGEST.fullmatch(record["sha256"]) is not None)

    def _validate_ledger(self):
        value = self._ledger
        if (not isinstance(value, dict) or set(value) != {"schema_version", "task_id", "logs", "summary", "pending"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["task_id"] != self.task_id
                or not isinstance(value["logs"], list) or len(value["logs"]) > 3
                or any(not self._valid_record(record, names=_LOG_NAMES, limit=MAX_LOG_BYTES) for record in value["logs"])
                or len({record["name"] for record in value["logs"]}) != len(value["logs"])
                or (value["summary"] is not None and not self._valid_record(value["summary"], names={"summary.json"}, limit=MAX_SUMMARY_BYTES))):
            raise _invalid()
        pending = value["pending"]
        if pending is not None:
            from .run_state import valid_temporary_record
            if (not isinstance(pending, dict) or set(pending) != {"name", "old", "temporary", "new", "phase"}
                    or pending["name"] not in {*_LOG_NAMES, "summary.json"}
                    or pending["phase"] not in {"allocated", "sealed", "published"}
                    or not valid_temporary_record(pending["temporary"], pending["name"])
                    or (pending["old"] is not None and not self._valid_record(pending["old"], names={pending["name"]}, limit=MAX_LOG_BYTES))
                    or (pending["phase"] != "allocated" and not self._valid_record(pending["new"], names={pending["name"]}, limit=MAX_LOG_BYTES))):
                raise _invalid()
            current = self._ledger["summary"] if pending["name"] == "summary.json" else next(
                (record for record in self._ledger["logs"] if record["name"] == pending["name"]), None)
            if current != (pending["new"] if pending["phase"] == "published" else pending["old"]):
                raise _invalid()

    def _read_registered(self, record, limit):
        raw = self._read_optional(record["name"], limit)
        if raw is None or raw[1] != record:
            raise _invalid()
        return raw[0]

    def _store_ledger(self):
        body = _json(self._ledger)
        if len(body) > MAX_SUMMARY_BYTES:
            raise _invalid()
        raw = self._read_optional("journal.json", MAX_SUMMARY_BYTES)
        if (raw[1] if raw else None) != self._ledger_record:
            raise _invalid()
        temporary = self.directory.allocate_temp("journal.json")
        try:
            temporary.write_stream(lambda stream: stream.write(body))
            raw = self._read_optional("journal.json", MAX_SUMMARY_BYTES)
            if (raw[1] if raw else None) != self._ledger_record:
                raise _invalid()
            if raw is None:
                self.directory.publish_temp(temporary)
            else:
                temporary._inspect(sealed=True)
                os.replace(temporary.name, "journal.json", src_dir_fd=self.directory._fd, dst_dir_fd=self.directory._fd)
                os.fsync(self.directory._fd)
            self._ledger_record = self._read_optional("journal.json", MAX_SUMMARY_BYTES)[1]
        finally:
            if self.directory.temporary_exists(temporary.name):
                self.directory.cleanup_temp(temporary)

    def _replace(self, name, body, old):
        if self._ledger["pending"] is not None:
            raise _invalid()
        if old is not None:
            self._read_registered(old, MAX_LOG_BYTES)
        else:
            self.directory.ensure_available([name])
        temporary = self.directory.allocate_temp(name)
        self._ledger["pending"] = {"name": name, "old": old, "temporary": temporary.record(), "new": None, "phase": "allocated"}
        self._store_ledger()
        temporary.write_stream(lambda stream: stream.write(body))
        record = temporary.record()
        self._ledger["pending"].update(temporary=record, phase="sealed", new={
            "name": name, **{key: record[key] for key in ("device", "inode", "size", "mtime_ns")}, "sha256": sha256(body).hexdigest()})
        self._store_ledger()
        self._recover_pending()

    def _recover_pending(self):
        self._validate_ledger()
        pending = self._ledger["pending"]
        if pending is None:
            return
        if pending["phase"] == "allocated":
            # No complete candidate was registered. Only this exact allocated
            # inode may be discarded; a substituted file is retained.
            current = self._read_optional(pending["name"], MAX_LOG_BYTES)
            if (current[1] if current else None) != pending["old"]:
                raise _invalid()
            if self.directory.temporary_exists(pending["temporary"]["name"]):
                temporary = self.directory.restore_temp(pending["temporary"])
                self.directory.cleanup_temp(temporary)
            self._ledger["pending"] = None
            self._store_ledger()
            return
        current = self._read_optional(pending["name"], MAX_LOG_BYTES)
        current_record = current[1] if current else None
        if current_record != pending["new"]:
            if pending["phase"] == "published" or current_record != pending["old"]:
                raise _invalid()
            temporary = self.directory.restore_temp(pending["temporary"])
            candidate = self._read_optional(temporary.name, MAX_LOG_BYTES)
            expected = {**pending["new"], "name": temporary.name}
            if candidate is None or candidate[1] != expected:
                raise _invalid()
            if pending["old"] is None:
                self.directory.publish_temp(temporary)
            else:
                self._read_registered(pending["old"], MAX_LOG_BYTES)
                temporary._inspect(sealed=True)
                os.replace(temporary.name, pending["name"], src_dir_fd=self.directory._fd, dst_dir_fd=self.directory._fd)
                os.fsync(self.directory._fd)
        if pending["phase"] != "published":
            if pending["name"] == "summary.json":
                self._ledger["summary"] = pending["new"]
            else:
                self._ledger["logs"] = [record for record in self._ledger["logs"] if record["name"] != pending["name"]] + [pending["new"]]
            pending["phase"] = "published"
            self._store_ledger()
        if self.directory.temporary_exists(pending["temporary"]["name"]):
            temporary = self.directory.restore_temp(pending["temporary"])
            self.directory.cleanup_temp(temporary)
        self._ledger["pending"] = None
        self._store_ledger()

    def emit(self, event):
        if not self._writable():
            return
        try:
            record = event_record(event)
            if not record:
                return
            body = _json(record)
            if len(body) > self.max_file_bytes:
                self._warn("DIAGNOSTIC_EVENT_TOO_LARGE", disable=False)
                return
            if self._buffer_bytes + len(body) > self.max_file_bytes:
                self.flush()
            if self.enabled:
                self._buffer.append(body)
                self._buffer_bytes += len(body)
        except Exception:
            self._warn("DIAGNOSTIC_WRITE_FAILED")

    def flush(self):
        if not self._writable() or not self._buffer:
            return
        try:
            logs = self._ledger["logs"]
            latest = logs[-1] if logs else None
            body = self._read_registered(latest, MAX_LOG_BYTES) if latest else b""
            if latest is not None and len(body) + self._buffer_bytes <= self.max_file_bytes:
                name, old = latest["name"], latest
            else:
                body = b""
                old = logs[0] if len(logs) == 3 else None
                name = old["name"] if old else next(name for name in _LOG_NAMES if name not in {entry["name"] for entry in logs})
            self._replace(name, body + b"".join(self._buffer), old)
            self._buffer, self._buffer_bytes = [], 0
        except Exception:
            self._warn("DIAGNOSTIC_WRITE_FAILED")

    def summary(self, result=None, unresolved=None):
        if result is None and unresolved is None:
            return self.read()["summary"]
        if not self._writable():
            return {}
        try:
            value = {"result": event_record(result), "unresolved": []}
            if isinstance(result, Mapping):
                for container in (result.get("run"), result.get("control")):
                    if isinstance(container, Mapping):
                        value["result"].update(event_record(container))
            if isinstance(unresolved, (list, tuple)):
                for entry in unresolved:
                    if isinstance(entry, Mapping):
                        value["unresolved"].append(_issue_record(entry))
            body = _json(value)
            if len(body) > MAX_SUMMARY_BYTES:
                raise _invalid()
            self._replace("summary.json", body, self._ledger["summary"])
            return deepcopy(value)
        except Exception:
            self._warn("DIAGNOSTIC_WRITE_FAILED")
            return {}

    def read(self):
        events, summary = [], {}
        if self.enabled and self._ledger["pending"] is None:
            try:
                for record in self._ledger["logs"]:
                    body = self._read_registered(record, MAX_LOG_BYTES)
                    for line in body.splitlines():
                        parsed = loads_json(line)
                        clean = event_record(parsed)
                        if not isinstance(parsed, dict) or clean != parsed:
                            raise _invalid()
                        events.append(clean)
                if self._ledger["summary"] is not None:
                    summary = loads_json(self._read_registered(self._ledger["summary"], MAX_SUMMARY_BYTES))
                    if (not isinstance(summary, dict) or set(summary) != {"result", "unresolved"}
                            or event_record(summary["result"]) != summary["result"]
                            or not isinstance(summary["unresolved"], list)
                            or any(_issue_record(item) != item for item in summary["unresolved"])):
                        raise _invalid()
            except Exception:
                events, summary = [], {}
                self._warn("DIAGNOSTIC_STATE_INVALID")
        return {"enabled": self.enabled, "events": events, "summary": summary, "warnings": deepcopy(self.warnings)}

    def close(self):
        if not self.read_only:
            self.flush()
