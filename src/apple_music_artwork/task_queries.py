"""Read-only task navigation and immutable admission of selected local tasks.

Navigation isolates damaged entries. It is not allocation authority: execution
still uses the strict library reconciliation before taking a task lock.
"""

from argparse import Namespace
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import os
from pathlib import Path
import re
import stat

from . import library
from .download import OutputDirectory
from .errors import ArtworkError
from .jsonio import MAX_JSON_BYTES, loads_json
from .run_state import (RunState, STATE_DIRECTORY, fingerprint, json_bytes,
                        valid_hash_record, valid_temporary_record)


_REFERENCE = re.compile(r"([0-9a-f]{64})(?:@([1-9][0-9]{0,3}))?")
_VERSION = re.compile(r"v([1-9][0-9]{0,3})\.json")
_TYPES = {"album", "artist", "playlist", "song"}


def stale():
    return ArtworkError("PLAN_STALE", "The selected task, directory or lock changed; inspect the current task list again.", "input")


def _invalid():
    return ArtworkError("RUN_STATE_INVALID", "The saved task is incomplete or has inconsistent identity; its files were retained.", "download")


def parse_reference(value, *, explicit_version=False):
    match = _REFERENCE.fullmatch(value) if isinstance(value, str) else None
    if match is None or explicit_version and match[2] is None:
        raise ArtworkError("INVALID_TASK_REFERENCE", "Use the complete task fingerprint and an explicit version when required.", "input")
    return match[1], int(match[2]) if match[2] else None


def _read(directory, name):
    try:
        return directory.read_file(name, max_bytes=MAX_JSON_BYTES)[0]
    except FileNotFoundError:
        return None


def _directory_identity(directory):
    info = os.fstat(directory._fd)
    return info.st_dev, info.st_ino


def _file_identity(value):
    return tuple(sorted(value.items())) if value is not None else None


def _validated_record(value):
    try:
        record = library._validate(value, value["task_id"])
        resource = record["identity"]["resource"]
        if (resource["resource_type"].removesuffix("s") not in _TYPES
                or not all(isinstance(resource[key], str) and resource[key] for key in ("resource_id", "storefront"))
                or not isinstance(record["identity"]["selection"], dict)):
            raise ValueError()
        return record
    except (KeyError, TypeError, ValueError, AttributeError):
        raise library._invalid() from None


def _validate_saved(saved, allocation=None):
    """Validate the fields queries consume, including snapshot/task agreement."""
    from .execution import _validate_base, _make_plan, _check_artifact, _error_value
    from .urls import parse_url
    try:
        if saved is None:
            raise ArtworkError("TASK_NOT_FOUND", "The directory does not contain a saved task.", "input")
        data = saved["payload"]
        identity = data["task_identity"]
        if (data["task_fingerprint"] != fingerprint(identity)
                or allocation is not None and (identity != allocation["identity"]
                    or data["task_fingerprint"] != allocation["task_id"])):
            raise _invalid()
        snapshot = data["snapshot"]
        manifest = snapshot["manifest"]
        if (not isinstance(snapshot["files"], list) or not isinstance(data["files"], dict)
                or snapshot.get("created_at") is not None and not isinstance(snapshot["created_at"], str)
                or manifest.get("resource_name") is not None and not isinstance(manifest["resource_name"], str)
                or not isinstance(data.get("history", []), list)
                or data.get("control") is not None and not isinstance(data["control"], dict)
                or type(data.get("layout_version", 1)) is not int or data.get("layout_version", 1) not in {1, 2}
                or "manifest_pending" not in data):
            raise _invalid()
        target = parse_url(manifest["target"]["input_url"])
        if target.key.to_dict() != identity["resource"] or target.locale != identity["locale"]:
            raise _invalid()
        args = Namespace(response=identity["source"]["kind"] == "offline",
                         provider=identity["provider"], **identity["options"],
                         **{"semantic" if key == "semantic_path" else key: value for key, value in identity["selection"].items()})
        _validate_base(manifest, args, target)
        plans, _ = _make_plan(manifest, snapshot["files"])
        if plans != snapshot["files"] or set(data["files"]) != {item["item_id"] for item in plans}:
            raise _invalid()
        for plan in plans:
            item = data["files"][plan["item_id"]]
            if (not isinstance(item, dict) or item.get("status") not in
                    {"pending", "running", "prepared", "committed", "failed", "cancelled", "not_started", "conflict", "skipped"}
                    or not {"status", "temporary", "artifact", "validation_policy"} <= item.keys()
                    or item["temporary"] is not None and not valid_temporary_record(item["temporary"], plan["filename"])):
                raise _invalid()
            if item["status"] in {"prepared", "committed"}:
                _check_artifact(plan, item["artifact"])
                if not isinstance(item["validation_policy"], str):
                    raise _invalid()
            if item.get("error") is not None:
                _error_value(item["error"])
        manifest_state = data["manifest"]
        if (not isinstance(manifest_state, dict) or not {"version", "current", "previous", "temporary"} <= manifest_state.keys()
                or type(manifest_state["version"]) is not int or manifest_state["version"] < 0
                or not valid_hash_record(manifest_state["current"], optional=True)
                or not valid_hash_record(manifest_state["previous"], optional=True)
                or manifest_state["temporary"] is not None and (not valid_temporary_record(manifest_state["temporary"], "manifest.json")
                    or manifest_state["temporary"]["sealed"] is not True)):
            raise _invalid()
        pending = data["manifest_pending"]
        if pending is not None and (not isinstance(pending, dict) or not {"version", "new", "old", "temporary"} <= pending.keys()
                or not valid_hash_record(pending["new"]) or not valid_hash_record(pending["old"], optional=True)
                or not valid_temporary_record(pending["temporary"], "manifest.json") or pending["temporary"]["sealed"] is not True
                or type(pending["version"]) is not int or pending["version"] != manifest_state["version"] + 1
                or pending["old"] != manifest_state["current"]):
            raise _invalid()
        lifecycle = (data.get("control") or {}).get("lifecycle", "unknown")
        if not isinstance(lifecycle, str):
            raise _invalid()
        return saved
    except (KeyError, TypeError, ValueError, AttributeError):
        raise _invalid() from None


def read_saved(output, allocation=None, *, recovery=False):
    if allocation is not None:
        with output.child(STATE_DIRECTORY) as metadata:
            body = _read(metadata, "allocation.json")
        if body is None or _validated_record(loads_json(body)) != allocation:
            raise library._invalid()
    saved = RunState.peek_recovery(output) if recovery else RunState.peek(output)
    _validate_saved(saved, allocation)
    # A candidate state must not authorize recovery of an unrecognized
    # manifest transaction. These are descriptor-relative metadata reads;
    # no media validation, temporary cleanup or transaction repair occurs.
    pending, manifest = saved["payload"]["manifest_pending"], saved["payload"]["manifest"]
    current = _read(output, "manifest.json")
    if pending is None:
        if not RunState._matches(current, manifest["current"]):
            raise ArtworkError("MANIFEST_CONFLICT", "The saved manifest is missing or changed; its files were retained.", "download")
    elif RunState._matches(current, pending["new"]):
        if pending["old"] is not None:
            with output.child(STATE_DIRECTORY) as metadata:
                if not RunState._matches(_read(metadata, "manifest.previous.json"), pending["old"]):
                    raise _invalid()
    elif RunState._matches(current, pending["old"]):
        temporary = output.restore_temp(pending["temporary"])
        if not RunState._matches(_read(output, temporary.name), pending["new"]):
            raise _invalid()
    else:
        raise ArtworkError("MANIFEST_CONFLICT", "The manifest differs from both registered versions; its files were retained.", "download")
    return saved


def _registry_value(root, allocation):
    try:
        with root.child(library.LIBRARY_DIRECTORY) as metadata, metadata.child("tasks") as tasks, tasks.child(allocation["task_id"]) as task:
            body = _read(task, f"v{allocation['version']}.json")
    except FileNotFoundError:
        return None
    if body is None:
        return None
    value = _validated_record(loads_json(body))
    if value != allocation:
        raise library._invalid()
    return json_bytes(value).decode("utf-8")


@dataclass(frozen=True)
class TaskBinding:
    """Private session value; all members are immutable, never public JSON."""
    path: str
    root: str | None
    root_identity: tuple | None
    directory_identity: tuple
    metadata_identity: tuple
    lock_identity: tuple
    identity_json: str
    allocation_json: str | None
    registry_json: str | None
    state_sha256: str
    state_revision: int
    state_version: int
    transition_sha256: str | None

    @property
    def task_ref(self):
        allocation = loads_json(self.allocation_json) if self.allocation_json else None
        return allocation["task_id"] + "@" + str(allocation["version"]) if allocation else None

    def admit(self, output, metadata=None, lock_fd=None):
        return validate_binding(self, output=output, metadata=metadata, lock_fd=lock_fd)


def capture_binding(path, allocation=None, *, root=None, recovery=True):
    """Capture identity and frozen state without opening, creating or taking a lock."""
    path = os.path.abspath(path)
    root_path, root_identity, registry = None, None, None
    if allocation is not None:
        _validated_record(allocation)
        if root is None:
            raise _invalid()
        root_path = os.path.abspath(root)
        if Path(path) != Path(root_path) / allocation["relative_path"]:
            raise _invalid()
        with OutputDirectory(root_path, create=False) as directory:
            root_identity = _directory_identity(directory)
            registry = _registry_value(directory, allocation)
    with OutputDirectory(path, create=False) as output:
        saved = read_saved(output, allocation, recovery=recovery)
        with output.child(STATE_DIRECTORY) as metadata:
            if allocation is not None:
                body = _read(metadata, "allocation.json")
                if body is None or _validated_record(loads_json(body)) != allocation:
                    raise library._invalid()
            lock = metadata.file_identity("lock")
            if lock is None:
                raise stale()
            transition = _read(metadata, "state-transition.json")
            return TaskBinding(path, root_path, root_identity, _directory_identity(output),
                               _directory_identity(metadata), _file_identity(lock),
                               json_bytes(saved["payload"]["task_identity"]).decode("utf-8"),
                               json_bytes(allocation).decode("utf-8") if allocation else None, registry,
                               saved["sha256"], saved["revision"], saved["state_version"],
                               sha256(transition).hexdigest() if transition is not None else None)


def validate_binding(binding, *, output=None, metadata=None, lock_fd=None):
    if not isinstance(binding, TaskBinding):
        raise stale()
    try:
        current = capture_binding(binding.path, loads_json(binding.allocation_json) if binding.allocation_json else None,
                                  root=binding.root)
        if current != binding:
            raise stale()
        if output is not None and _directory_identity(output) != binding.directory_identity:
            raise stale()
        if metadata is not None and _directory_identity(metadata) != binding.metadata_identity:
            raise stale()
        if lock_fd is not None:
            locked = os.fstat(lock_fd)
            identity = dict(binding.lock_identity)
            if not stat.S_ISREG(locked.st_mode) or (locked.st_dev, locked.st_ino) != (identity["device"], identity["inode"]):
                raise stale()
    except (ArtworkError, OSError, KeyError, TypeError, ValueError):
        raise stale() from None


@contextmanager
def open_bound_output(binding):
    validate_binding(binding)
    try:
        with OutputDirectory(binding.path, create=False) as output:
            validate_binding(binding, output=output)
            yield output
    except FileNotFoundError:
        raise stale() from None


def _scan(root_path):
    """Bounded, independently validated allocation observations for navigation."""
    candidates, invalid, errors, maxima, seen_paths = {}, set(), [], {}, {}
    visited = 0

    def failure(error, path, key=None):
        if key is not None:
            invalid.add(key)
        # One damaged local allocation and its matching registry observation
        # represent the same damaged entry, not two errors.
        entry = {"relative_path": str(path), "error": error.to_dict()}
        if key is not None:
            entry.update(task_id=key[0], version=key[1])
        if not any(item["relative_path"] == entry["relative_path"] for item in errors):
            errors.append(entry)

    def accept(record, origin):
        key = record["task_id"], record["version"]
        maxima[key[0]] = max(maxima.get(key[0], 0), key[1])
        prior = candidates.get(key)
        if prior is not None and prior != record:
            failure(library._invalid(), record["relative_path"], key)
        else:
            candidates[key] = record
        if origin == "local":
            previous = seen_paths.get(record["relative_path"])
            if previous is not None and previous != key:
                invalid.add(previous)
                failure(library._invalid(), record["relative_path"], key)
            seen_paths[record["relative_path"]] = key

    def visit(directory, parts):
        nonlocal visited
        visited += 1
        if visited > library.MAX_TASKS * 4:
            raise ArtworkError("LIBRARY_SCAN_LIMIT", "Task query exceeds the bounded directory scan limit.", "download")
        if len(parts) == 4:
            relative = str(Path(*parts))
            try:
                with directory.child(STATE_DIRECTORY) as metadata:
                    body = _read(metadata, "allocation.json")
                if body is None:
                    return
                record = _validated_record(loads_json(body))
                if record["relative_path"] != relative:
                    raise library._invalid()
                accept(record, "local")
            except FileNotFoundError:
                return
            except (ArtworkError, OSError) as error:
                failure(error if isinstance(error, ArtworkError) else library._invalid(), relative)
                seen_paths[relative] = None
            return
        names = directory.names()
        if len(names) > library.MAX_TASKS:
            raise library._invalid()
        for name in sorted(names):
            if not parts and name not in _TYPES or parts and name.startswith("."):
                continue
            try:
                info = os.stat(name, dir_fd=directory._fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise library._invalid()
                if not stat.S_ISDIR(info.st_mode):
                    continue
                with directory.child(name) as child:
                    visit(child, [*parts, name])
            except (ArtworkError, OSError) as error:
                failure(error if isinstance(error, ArtworkError) else library._invalid(), Path(*parts, name))
                if isinstance(error, ArtworkError) and error.code == "LIBRARY_SCAN_LIMIT":
                    raise

    try:
        with OutputDirectory(root_path, create=False) as root:
            visit(root, [])
            try:
                with root.child(library.LIBRARY_DIRECTORY) as metadata, metadata.child("tasks") as tasks:
                    names = tasks.names()
                    if len(names) > library.MAX_TASKS:
                        raise library._invalid()
                    for task_id in sorted(names):
                        relative = Path(library.LIBRARY_DIRECTORY, "tasks", task_id)
                        try:
                            parse_reference(task_id, explicit_version=False)
                            if "@" in task_id:
                                raise library._invalid()
                            with tasks.child(task_id) as task:
                                versions = task.names()
                                if len(versions) > library.MAX_VERSIONS * 2:
                                    raise library._invalid()
                                for name in sorted(versions):
                                    match = _VERSION.fullmatch(name)
                                    if match is None:
                                        if re.fullmatch(r"ready-v[1-9][0-9]{0,3}\.json", name):
                                            continue
                                        failure(library._invalid(), relative / name)
                                        continue
                                    key = task_id, int(match[1])
                                    maxima[task_id] = max(maxima.get(task_id, 0), key[1])
                                    try:
                                        record = _validated_record(loads_json(_read(task, name)))
                                        if (record["task_id"], record["version"]) != key:
                                            raise library._invalid()
                                        accept(record, "registry")
                                    except (ArtworkError, OSError) as error:
                                        failure(error if isinstance(error, ArtworkError) else library._invalid(), relative / name, key)
                        except (ArtworkError, OSError) as error:
                            failure(error if isinstance(error, ArtworkError) else library._invalid(), relative)
            except FileNotFoundError:
                pass
            except (ArtworkError, OSError) as error:
                failure(error if isinstance(error, ArtworkError) else library._invalid(), library.LIBRARY_DIRECTORY)
    except FileNotFoundError:
        return [], [], {}
    # A retained but damaged local allocation never becomes a selectable
    # registry-only row. A genuinely unfinished reservation may remain a row.
    for key, record in candidates.items():
        if record["relative_path"] in seen_paths and seen_paths[record["relative_path"]] is None:
            invalid.add(key)
    return [record for key, record in candidates.items() if key not in invalid], errors, maxima


def _selection_label(selection):
    return ("all-media" if selection.get("all_media") else "all-motion" if selection.get("all_motion") else
            "all-static" if selection.get("all_static") else "asset" if selection.get("asset_id") else
            "semantic" if selection.get("semantic_path") else "primary")


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def query_tasks(root, *, query=None, resource_type=None, statuses=(), latest=False):
    if (query is not None and not isinstance(query, str) or resource_type is not None and resource_type not in _TYPES
            or not isinstance(statuses, (list, tuple)) or any(not isinstance(item, str) for item in statuses)
            or type(latest) is not bool):
        raise ArtworkError("INVALID_ARGUMENTS", "Invalid task query filters.", "input")
    records, errors, maxima = _scan(root)
    rows, bindings = [], {}
    for record in records:
        if latest and record["version"] != maxima[record["task_id"]]:
            continue
        identity, resource = record["identity"], record["identity"]["resource"]
        reference = record["task_id"] + "@" + str(record["version"])
        row = {"task_id": record["task_id"], "version": record["version"], "task_ref": reference,
               "relative_path": record["relative_path"], "parent": record.get("parent"), "lifecycle": "allocated",
               "resource_name": None, "resource_type": resource["resource_type"].removesuffix("s"),
               "resource_id": resource["resource_id"], "storefront": resource["storefront"],
               "locale": identity.get("locale"), "selection": deepcopy(identity["selection"]),
               "selection_label": _selection_label(identity["selection"]), "snapshot_created_at": None,
               "selectable": False, "recovery_required": False}
        path = Path(root) / record["relative_path"]
        try:
            with OutputDirectory(path, create=False) as output:
                saved = read_saved(output, record, recovery=True)
            data = saved["payload"]
            row.update(resource_name=data["snapshot"]["manifest"].get("resource_name"),
                       snapshot_created_at=data["snapshot"].get("created_at"),
                       recovery_required=bool(saved.get("recovery_required") or data.get("manifest_pending")),
                       lifecycle=(data.get("control") or {}).get("lifecycle", "unknown"))
            binding = capture_binding(path, record, root=root)
            if binding.state_sha256 != saved["sha256"]:
                raise stale()
            row["selectable"] = True
            bindings[reference] = binding
        except (ArtworkError, OSError) as error:
            row["error"] = (error if isinstance(error, ArtworkError) else _invalid()).to_dict()
        if (query is not None and not any(query.casefold() in str(row.get(key) or "").casefold()
                                          for key in ("resource_name", "resource_id", "task_id"))
                or resource_type is not None and row["resource_type"] != resource_type
                or statuses and row["lifecycle"] not in statuses):
            bindings.pop(reference, None)
            continue
        rows.append(row)
    rows.sort(key=lambda row: (_timestamp(row["snapshot_created_at"]) is None,
                              -(_timestamp(row["snapshot_created_at"]) or 0), row["task_id"], row["version"]))
    return {"tasks": rows, "errors": errors}, bindings


def query_location(root, reference):
    """Navigate a full identity without making other damaged entries disappear."""
    task_id, version = parse_reference(reference)
    records, errors, maxima = _scan(root)
    wanted = maxima.get(task_id) if version is None else version
    record = next((item for item in records if item["task_id"] == task_id and item["version"] == wanted), None)
    if record is None:
        raise ArtworkError("TASK_NOT_FOUND", "The requested task has no independently readable allocation.", "input")
    return Path(root) / record["relative_path"], record
