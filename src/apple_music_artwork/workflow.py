"""Application commands shared by the CLI and non-interactive callers."""

from argparse import Namespace
from copy import deepcopy
from pathlib import Path
import os
import re
from uuid import uuid4

from .download import OutputDirectory
from .errors import ArtworkError
from .execution import execute, EXIT_CODES
from .jsonio import load_json, loads_json
from . import library
from .planning import preflight, selection_label, validate_plan
from .protocol import envelope, validate_choice
from .run_state import RunState, STATE_DIRECTORY, fingerprint
from .urls import parse_url
from .task_queries import (capture_binding, open_bound_output, query_location, query_tasks,
                           read_saved, stale, validate_binding, parse_reference)


_DEFAULTS = {"command": "download", "url": None, "response": None, "output": None, "list": False,
             "semantic": None, "asset_id": None, "all_static": False, "all_media": False, "all_motion": False,
             "inspect_motion": False, "compare_storefronts": None, "token_env": "APPLE_MUSIC_DEVELOPER_TOKEN",
             "provider": "web", "width": None, "height": None, "format": None, "motion_profile": "best",
             "jobs": 2, "jobs_explicit": False, "json": False, "progress": "auto", "library": None,
             "new_version": False, "plan_file": None, "decisions_file": None, "task_ref": None,
             "task_dir": None, "no_input": False, "events_jsonl": False,
             "query": None, "resource_type": None, "statuses": (), "latest": False,
             "left_ref": None, "right_ref": None}


def _args(args):
    return Namespace(**{**_DEFAULTS, **vars(args)})


def _location(args, *, read_only=False):
    if args.task_dir:
        return Path(args.task_dir), None
    if not isinstance(args.task_ref, str):
        raise ArtworkError("TASK_REFERENCE_REQUIRED", "Specify a task directory or complete library task reference.", "input")
    match = re.fullmatch(r"([0-9a-f]{64})(?:@([1-9][0-9]{0,3}))?", args.task_ref)
    if match is None:
        raise ArtworkError("INVALID_TASK_REFERENCE", "Use a complete task fingerprint, optionally followed by @version.", "input")
    root = args.library or "output"
    if read_only:
        return query_location(root, args.task_ref)
    record = library.resolve(root, match[1], version=int(match[2]) if match[2] else None)
    if record is None:
        raise ArtworkError("TASK_NOT_FOUND", "The requested task is not registered in this library.", "input")
    return Path(root) / record["relative_path"], record


def _state_at(path, allocation=None):
    try:
        with OutputDirectory(path, create=False) as output:
            saved = read_saved(output, allocation)
    except FileNotFoundError:
        raise ArtworkError("TASK_NOT_FOUND", "The task directory does not exist.", "input") from None
    if saved is None:
        raise ArtworkError("TASK_NOT_FOUND", "The directory does not contain a saved task.", "input")
    data = saved["payload"]
    if data.get("task_fingerprint") != fingerprint(data.get("task_identity")):
        raise ArtworkError("RUN_STATE_INVALID", "The saved task identity is inconsistent.", "download")
    return saved


def _restore_args(args, path, saved):
    """Resume parsed facts, never reload or preserve an original response dump."""
    data, result = saved["payload"], _args(args)
    identity = data["task_identity"]
    result.url = data["snapshot"]["manifest"]["target"]["input_url"]
    result.output, result.library, result.new_version = str(path), None, False
    result.response = "frozen-offline-snapshot" if identity["source"]["kind"] == "offline" else None
    result.provider = identity["provider"] if identity["provider"] != "offline" else "web"
    result._resume_identity = deepcopy(identity)
    for name, value in identity["options"].items():
        if name != "crop":
            setattr(result, name, value)
    for name, value in identity["selection"].items():
        setattr(result, "semantic" if name == "semantic_path" else name, value)
    return result


def _recover_execution(path, args, expected_identity=None):
    """Actual execution may repair metadata after verifying the frozen identity."""
    from .execution import _validate_base
    try:
        binding = getattr(args, "_selection_binding", None)
        with (open_bound_output(binding) if binding is not None else OutputDirectory(path, create=False)) as output:
            saved = RunState.peek_recovery(output)
            if saved is None or not (saved.get("recovery_required") or saved["payload"].get("manifest_pending")):
                return False
            data = saved["payload"]
            if (data.get("task_fingerprint") != fingerprint(data.get("task_identity"))
                    or expected_identity is not None and data["task_identity"] != expected_identity):
                raise ArtworkError("TASK_CONFLICT", "The interrupted transaction belongs to different task parameters.", "download")
            restored = _restore_args(args, path, saved)
            _validate_base(data["snapshot"]["manifest"], restored, parse_url(restored.url))
            with RunState(output, admission=binding.admit if binding is not None else None,
                          require_existing_lock=binding is not None) as state:
                if state.data["task_identity"] != data["task_identity"]:
                    raise ArtworkError("TASK_CONFLICT", "The task changed while acquiring its recovery lock.", "download")
                state.recover_manifest()
                if binding is not None:
                    args._selection_binding = capture_binding(path,
                        loads_json(binding.allocation_json) if binding.allocation_json else None, root=binding.root)
            return True
    except FileNotFoundError:
        return False


def _choices(path):
    if path is None:
        return []
    value = load_json(path)
    if (not isinstance(value, dict) or set(value) != {"protocol_version", "choices"}
            or type(value["protocol_version"]) is not int or value["protocol_version"] != 1
            or not isinstance(value["choices"], list) or len(value["choices"]) > 256):
        raise ArtworkError("INVALID_DECISION", "Use a version 1 choices envelope with a bounded choice list.", "input")
    ids = set()
    for choice in value["choices"]:
        if (not isinstance(choice, dict) or not {"plan_id", "decision_id", "action"} <= choice.keys()
                or set(choice) - {"plan_id", "decision_id", "action", "params"}
                or not all(isinstance(choice.get(key), str) for key in ("plan_id", "decision_id", "action"))
                or choice["decision_id"] in ids):
            raise ArtworkError("INVALID_DECISION", "Choices must have unique decision IDs and explicit plan bindings.", "input")
        ids.add(choice["decision_id"])
    return value["choices"]


def _loaded_plan(path):
    value = load_json(path)
    if isinstance(value, dict) and value.get("kind") == "plan":
        if value.get("schema_version") != 1 or value.get("status") != "success":
            raise ArtworkError("PLAN_INVALID", "Only a successful version 1 plan envelope can be executed.", "input")
        value = value.get("data")
    return validate_plan(value)


def _plan_args(args, plan):
    """A saved plan is self-contained; explicit contradicting options fail."""
    args = _args(args)
    identity, destination = plan["identity"], plan["destination"]
    expected = {"url": plan["target"]["input_url"], "provider": identity["provider"] if identity["provider"] != "offline" else "web",
                "new_version": bool(destination.get("new_version", destination.get("expected_allocation") and destination.get("allocation") is None)),
                "response": "frozen-offline-snapshot" if identity["source"]["kind"] == "offline" else None,
                "output": destination["path"] if destination["mode"] == "exact" else None,
                "library": destination["library_root"] if destination["mode"] == "library" else None,
                **{key: value for key, value in identity["options"].items() if key != "crop"},
                **{"semantic" if key == "semantic_path" else key: value for key, value in identity["selection"].items()}}
    explicit = getattr(args, "explicit_options", None)
    if explicit is None:
        explicit = {key for key in expected if getattr(args, key, None) != _DEFAULTS.get(key)}
    for key in set(explicit) & set(expected):
        value = getattr(args, key, None)
        matches = value == expected[key]
        if key == "url":
            target, wanted = parse_url(value), parse_url(expected[key])
            matches = target.key == wanted.key and target.locale == wanted.locale
        elif key in {"output", "library"}:
            matches = bool(value) and expected[key] is not None and os.path.abspath(value) == os.path.abspath(expected[key])
        elif key == "response":
            matches = identity["source"]["kind"] == "offline" and fingerprint(load_json(value)) == identity["source"]["content_sha256"]
        if not matches:
            raise ArtworkError("PLAN_STALE", "Explicit arguments conflict with the saved plan; create a new plan.", "input")
    result = _args(args)
    for key, value in expected.items():
        setattr(result, key, value)
    result._resume_identity = deepcopy(identity)
    result.new_version = expected["new_version"]
    return result


class _EventStream:
    """One identity and sequence across preflight, list, and execution events."""
    def __init__(self, sink):
        self.sink, self.sequence = sink, 0
        self.run_id, self.task_id, self.plan_id = str(uuid4()), None, None

    def prepare(self, event):
        self.sequence += 1
        self.task_id = event.get("task_id", self.task_id)
        self.plan_id = event.get("plan_id", self.plan_id)
        return {**event, "schema_version": 1, "sequence": self.sequence, "run_id": self.run_id,
                "task_id": self.task_id, "plan_id": self.plan_id}

    def deliver(self, event):
        if self.sink is not None:
            self.sink(event)

    def __call__(self, event):
        self.deliver(self.prepare(event))

    def flush(self):
        flush = getattr(self.sink, "flush", None)
        if callable(flush):
            flush()


def _task_query(path, allocation=None):
    saved = _state_at(path, allocation)
    data = saved["payload"]
    return {"task_id": data["task_fingerprint"], "identity": data["task_identity"],
            "layout_version": data.get("layout_version", 1), "state_version": saved["state_version"],
            "version": allocation["version"] if allocation else None,
            "control": data.get("control"), "snapshot": data["snapshot"], "items": data["files"],
            "history": data.get("history", [])}


def _derived_args(args, plan, request):
    from .execution import _options
    child = _plan_args(Namespace(command="download"), plan)
    params = request["params"]
    child.library = params.get("library", plan["destination"]["library_root"])
    child.output, child.new_version = None, False
    for name in ("width", "height", "format", "motion_profile"):
        if name in params:
            setattr(child, name, params[name])
    if "selection" in params:
        child.semantic = child.asset_id = None
        child.all_static = params["selection"] == "all_static"
        child.all_media = child.all_motion = False
    identity = deepcopy(plan["identity"])
    identity["options"] = _options(child)
    identity["selection"] = {"semantic_path": child.semantic, "asset_id": child.asset_id,
                             "all_static": child.all_static, "all_media": child.all_media, "all_motion": child.all_motion}
    if fingerprint(identity) == plan["task_id"]:
        raise ArtworkError("INVALID_DECISION", "A derived task must change the selected parameters.", "input")
    child._resume_identity = identity
    child._source_manifest = deepcopy(plan["snapshot"]["manifest"])
    child._source_created_at = plan["snapshot"]["created_at"]
    child._parent_record = deepcopy(request["parent"])
    child_id = fingerprint(identity)
    latest = library.resolve(child.library, child_id)
    if latest is not None:
        related = [record for record in library.list_tasks(child.library)
                   if record["task_id"] == child_id and record.get("parent") == child._parent_record]
        if len(related) > 1:
            raise ArtworkError("RUN_STATE_INVALID", "A derived decision has conflicting child versions.", "download")
        if not related:
            child.new_version = True
        elif related[0]["version"] != latest["version"]:
            # A repeat of an older parent decision resumes its own child,
            # even after another parent has explicitly derived a newer one.
            child._library_root = os.path.abspath(child.library)
            child._library_allocation = related[0]
            child.output = str(Path(child._library_root) / related[0]["relative_path"])
            child.library = None
    child.plan_file = child.decisions_file = None
    child.json, child.no_input, child.events_jsonl, child.jobs = args.json, args.no_input, args.events_jsonl, args.jobs
    child.explicit_options = []
    return child


def _dispatch(args, *, http, environ, media_tools=None, event_sink=None, decision_provider=None):
    command = args.command
    supplied_plan = None
    if command == "tasks.diff":
        from .comparison import compare_snapshots
        from .remote_check import snapshot_reference
        if args.task_dir or args.task_ref:
            raise ArtworkError("INVALID_TASK_REFERENCE", "Diff requires two explicit version references.", "input")
        left_id, _ = parse_reference(args.left_ref, explicit_version=True)
        right_id, _ = parse_reference(args.right_ref, explicit_version=True)
        if left_id != right_id:
            raise ArtworkError("TASK_IDENTITY_MISMATCH", "Compare versions of the same complete task identity.", "input")
        left = _task_query(*query_location(args.library or "output", args.left_ref))
        right = _task_query(*query_location(args.library or "output", args.right_ref))
        if left["identity"] != right["identity"]:
            raise ArtworkError("TASK_IDENTITY_MISMATCH", "Saved versions have different task identities.", "input")
        data = {"task_id": left_id, "left": snapshot_reference(left), "right": snapshot_reference(right),
                **compare_snapshots(left["snapshot"], right["snapshot"]),
                "network_scope": {"resource_metadata": False, "hls_and_head": False, "media_bodies": False}}
        return envelope(command, data), 0
    if command == "tasks.check-updates":
        from .remote_check import check_updates
        if args.task_ref and args.task_dir:
            raise ArtworkError("INVALID_TASK_REFERENCE", "Specify one task reference or one exact task directory.", "input")
        path, allocation = _location(args, read_only=True)
        data, errors, code = check_updates(_task_query(path, allocation), http=http, environ=environ,
                                          token_env=args.token_env)
        return envelope(command, data, status="failed" if errors else "success", errors=errors), code
    if command == "doctor":
        from .diagnostics import doctor
        result = doctor(environ=environ)
        if args.task_dir or args.task_ref:
            path, allocation = _location(args, read_only=True)
            task = _task_query(path, allocation)
            result["task_id"] = task["task_id"]
            result["required_capabilities"] = ["static_download"]
            if any(plan["kind"] == "video" and task["items"][plan["item_id"]]["status"] != "committed"
                   for plan in task["snapshot"]["files"]):
                result["required_capabilities"].append("motion_download")
        return envelope("doctor", result), 0
    if command == "tasks.list":
        data, _ = query_tasks(args.library or "output", query=args.query, resource_type=args.resource_type,
                              statuses=args.statuses, latest=args.latest)
        return envelope("tasks.list", data), 0
    if command in {"tasks.show", "diagnostics"}:
        path, allocation = _location(args, read_only=True)
        if command == "tasks.show":
            return envelope(command, _task_query(path, allocation)), 0
        saved = _state_at(path, allocation)
        from .diagnostics import EventJournal
        try:
            with OutputDirectory(path, create=False) as output, output.child(STATE_DIRECTORY) as metadata, metadata.child("diagnostics") as directory:
                result = EventJournal(directory, task_id=saved["payload"]["task_fingerprint"], read_only=True).read()
        except FileNotFoundError:
            result = {"enabled": False, "events": [], "summary": None, "warnings": []}
        return envelope(command, result), 0
    allocation = None
    if command == "resume" or (command == "plan" and args.task_dir):
        selected = getattr(args, "_selection_binding", None)
        if selected is not None:
            validate_binding(selected)
            if (args.task_ref != selected.task_ref or os.path.abspath(args.library or "output") != selected.root
                    or args.task_dir):
                raise stale()
        path, allocation = _location(args)
        if command == "resume":
            if selected is not None and os.path.abspath(path) != selected.path:
                raise stale()
            if args.plan_file:
                supplied_plan = _loaded_plan(args.plan_file)
                if os.path.abspath(path) != supplied_plan["destination"]["path"]:
                    raise stale()
                if supplied_plan["local_observations"]["exists"] and supplied_plan["local_observations"]["state_sha256"] is not None:
                    from .planning import open_existing_plan_output
                    with open_existing_plan_output(supplied_plan):
                        pass
            recovered = (_recover_execution(path, args, allocation["identity"] if allocation else None)
                         if not args.plan_file else False)
            if recovered and (args.plan_file or args.decisions_file):
                raise ArtworkError("PLAN_STALE", "Task metadata was recovered; request a fresh plan before submitting choices.", "input")
        args = _restore_args(args, path, _state_at(path, allocation))
        if allocation is not None:
            args._library_allocation = allocation
            args._library_root = str(Path(path).parents[3])
    if args.list:
        return execute(args, http=http, environ=environ, media_tools=media_tools, event_sink=event_sink)
    if args.plan_file:
        plan = supplied_plan or _loaded_plan(args.plan_file)
        args = _plan_args(args, plan)
    else:
        if command == "download" and not args.new_version:
            from .execution import _task_identity
            expected = deepcopy(getattr(args, "_resume_identity", None)) or _task_identity(args, parse_url(args.url), load_json(args.response) if args.response else None)
            recovery_path = args.output
            if recovery_path is None:
                registered = library.resolve(args.library or "output", fingerprint(expected))
                recovery_path = Path(args.library or "output") / registered["relative_path"] if registered else None
            recovered = _recover_execution(recovery_path, args, expected) if recovery_path is not None else False
            if recovered and args.decisions_file:
                raise ArtworkError("PLAN_STALE", "Task metadata was recovered; request a fresh plan before submitting choices.", "input")
        plan = preflight(args, http=http, environ=environ, media_tools=media_tools)
    if command == "plan":
        return envelope("plan", plan), 0
    if plan["local_observations"]["exists"] and plan["local_observations"]["state_sha256"] is not None:
        from .planning import open_existing_plan_output
        with open_existing_plan_output(plan):
            pass
    blocker = next((check for check in plan["checks"] if check.get("startup_blocker")), None)
    if blocker is not None:
        from .execution import _error_value
        error = _error_value(blocker["error"])
        error.manifest = deepcopy(plan["snapshot"]["manifest"])
        error.manifest["preflight_checks"] = plan["checks"]
        raise error
    if event_sink is not None:
        event_sink({"kind": "plan", "schema_version": 1, "plan": plan, "plan_id": plan["plan_id"], "task_id": plan["task_id"]})
    choices = _choices(args.decisions_file)
    if choices:
        contexts = {item["decision_id"]: item for item in plan["decisions"]}
        try:
            saved = _state_at(plan["destination"]["path"])
            contexts.update({item["decision_id"]: item for item in (saved["payload"].get("control") or {}).get("decisions", [])})
        except ArtworkError as error:
            if error.code != "TASK_NOT_FOUND":
                raise
        for choice in choices:
            if choice["decision_id"] not in contexts:
                raise ArtworkError("DECISION_STALE", "A supplied choice does not match any current decision.", "input")
            validate_choice(contexts[choice["decision_id"]], choice)
    created_directory_identity = None
    if plan["destination"]["mode"] == "library" and not plan["destination"].get("resume_existing"):
        destination = plan["destination"]
        record = library.allocate(destination["library_root"], plan["identity"], plan["snapshot"]["manifest"].get("resource_name"),
                                  selection_label(args), new_version=args.new_version,
                                  expected=destination["expected_allocation"],
                                  expected_path=str(Path(destination["path"]).relative_to(destination["library_root"])),
                                  parent=getattr(args, "_parent_record", None))
        if str(Path(destination["library_root"]) / record["relative_path"]) != destination["path"]:
            raise ArtworkError("PLAN_STALE", "Library allocation no longer matches the selected plan.", "input")
        if not plan["local_observations"]["exists"]:
            with OutputDirectory(destination["path"], create=False) as output:
                info = os.fstat(output._fd)
                created_directory_identity = {"device": info.st_dev, "inode": info.st_ino}
    provider = decision_provider if not (args.json or args.no_input or args.events_jsonl) else None
    result, code = execute(args, http=http, environ=environ, media_tools=media_tools, event_sink=event_sink,
                           plan=plan, decision_provider=provider, choices=choices,
                           created_directory_identity=created_directory_identity)
    next_task = result.get("control", {}).get("next_task")
    if code == 9 and result.get("manifest_report", {}).get("status") == "saved" and next_task is not None:
        try:
            child = _derived_args(args, plan, next_task)
            http.control.begin_attempt_window()
            return _dispatch(child, http=http, environ=environ, media_tools=media_tools,
                             event_sink=event_sink, decision_provider=decision_provider)
        except ArtworkError as error:
            # The parent is already durable. A child that cannot start must
            # retain that parent's identity, completed files and resume entry.
            result = deepcopy(result)
            result["download_status"] = result["status"]
            result.update(status="failed", status_scope="derived_task_start")
            result["errors"].append(error.to_dict())
            result["control"]["derived_task_start"] = {"status": "failed", "error": error.to_dict()}
            return result, 130 if error.code == "CANCELLED" else EXIT_CODES.get(error.category, 1)
    return result, code


def dispatch(args, *, http, environ, media_tools=None, event_sink=None, decision_provider=None):
    args = _args(args)
    event_sink = _EventStream(event_sink)
    try:
        return _dispatch(args, http=http, environ=environ, media_tools=media_tools,
                         event_sink=event_sink, decision_provider=decision_provider)
    except ArtworkError as error:
        code = 130 if error.code == "CANCELLED" else EXIT_CODES.get(error.category, 1)
        if args.command not in {"download", "resume"}:
            return envelope(args.command, status="failed", errors=[error.to_dict()]), code
        if hasattr(error, "manifest"):
            from .execution import _coverage, _status
            result = error.manifest
            result["errors"].append(error.to_dict())
            _status(result)
            _coverage(result)
            result["manifest_report"] = {"path": "manifest.json", "status": "not_saved"}
            return result, code
        return {"schema_version": 2, "mode": "download", "status": "failed", "errors": [error.to_dict()], "downloads": [],
                "manifest_report": {"path": "manifest.json", "status": "not_saved"},
                "control": {"protocol_version": 1, "lifecycle": "cancelled" if code == 130 else "failed", "task_id": None,
                            "plan_id": None, "decision_required": False, "decisions": [], "resume": None}}, code
