"""Read-only preparation and exact local observations for execution admission."""

from copy import deepcopy
from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import stat

from .client import AppleMusicClient, MAX_IMAGE_BYTES, MAX_MOTION_BYTES
from .download import OutputDirectory
from .errors import ArtworkError
from .jsonio import MAX_JSON_BYTES, load_json, loads_json
from .library import proposed_path, resolve
from .models import DiscoveryResult, SourceEvidence, StorefrontObservation
from .protocol import decision
from .run_state import RunState, STATE_DIRECTORY, fingerprint, utc_now
from .selection import select
from .urls import parse_url


def selection_label(args):
    return ("all-media" if args.all_media else "all-motion" if args.all_motion else
            "all-static" if args.all_static else "asset" if args.asset_id else
            "semantic" if args.semantic else "primary")


def derived_options(plan, item_ids):
    """Offer exact changes; choosing one creates another task identity."""
    problems = [item for item in plan["items"] if item["item_id"] in item_ids]
    kinds = {item["kind"] for item in problems}
    extra = {"library": plan["destination"]["derive_library_root"]} if plan["destination"]["mode"] == "exact" else {}
    suffix = f"；在资料库 {extra['library']} 新建关联任务" if extra else "；在当前资料库新建关联任务"
    options = []
    if "video" in kinds and plan["identity"]["options"]["motion_profile"] == "best":
        options.append({"action": "derive", "label": "改为 H.264 compatible" + suffix,
                        "params": {**extra, "motion_profile": "compatible"}})
    if "video" in kinds and any(asset["kind"] == "image" and asset["role"] != "unknown" for asset in plan["snapshot"]["manifest"]["assets"]):
        options.append({"action": "derive", "label": "改为只取图片" + suffix,
                        "params": {**extra, "selection": "all_static"}})
    widths = [item["spec"]["width"] for item in problems if item["kind"] == "image" and item.get("spec")
              and type(item["spec"].get("width")) is int and item["spec"]["width"] > 1]
    if widths:
        width = max(1, min(widths) // 2)
        options.append({"action": "derive", "label": f"新任务全部图片宽度改为 {width} 像素，按声明比例取图" + suffix,
                        "params": {**extra, "width": width, "height": None}})
    return options


def seal(plan):
    body = {key: value for key, value in plan.items() if key not in {"plan_id", "decisions"}}
    plan["plan_id"] = fingerprint(body)
    plan["decisions"] = [decision(plan["plan_id"], "item_issue", item["error"]["code"], [item["item_id"]],
                                  (["retry", "defer", "skip"] if item["error"]["code"] not in {"OUTPUT_CONFLICT", "OUTPUT_IDENTITY_CHANGED"}
                                  else ["defer", "skip"]) + derived_options(plan, [item["item_id"]]))
                         for item in plan["items"] if item["action"] == "needs_action"]
    for context in plan["decisions"]:
        context["display"] = {"items": [{key: item.get(key) for key in ("name", "name_key", "kind", "spec")}
                                        for item in plan["items"] if item["item_id"] in context["item_ids"]]}
    return plan


def validate_plan(plan):
    try:
        if (not isinstance(plan, dict) or type(plan.get("protocol_version")) is not int
                or plan.get("protocol_version") != 1 or plan["task_id"] != fingerprint(plan["identity"])
                or seal(deepcopy(plan)) != plan):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ArtworkError("PLAN_INVALID", "The saved plan is malformed or its content fingerprint changed.", "input") from None
    return plan


def revalidate(plan, output, state, *, created_directory_identity=None):
    """Run after taking the task lock and before any media or state mutation."""
    validate_plan(plan)
    envelope = ({"payload": state.data, "revision": state.revision, "sha256": state.current_sha256}
                if state.data is not None else None)
    current = observe(output, plan["snapshot"], plan["destination"]["layout_version"], state=envelope, check_busy=False)
    expected = deepcopy(plan["local_observations"])
    expected["busy"] = False
    if not expected["exists"] and (output.created or created_directory_identity == current["directory"]):
        # Allocation/opening may create the previously absent directory and
        # lock, but grants no authority over a newly appeared file or state.
        current["exists"], current["directory"] = False, None
    if current != expected:
        updated = deepcopy(plan)
        updated["local_observations"] = current
        by_id = {item["item_id"]: item for item in current["files"]}
        for item in updated["items"]:
            observed = by_id.get(item["item_id"])
            if observed is None:
                continue
            if observed["status"] == "conflict":
                item.update(action="needs_action", error=observed.get("error") or ArtworkError(
                    "OUTPUT_CONFLICT", "Local media differs from the verified record; it will be preserved.", "download").to_dict())
            elif observed["status"] in {"reusable", "validate_local"}:
                item["action"] = "reuse" if observed["status"] == "reusable" else "validate_local"
                item.pop("error", None)
            elif item["action"] == "reuse":
                item["action"] = "download"
                item.pop("error", None)
        for check in updated["checks"]:
            if check["id"] == "space":
                check["known_required_bytes"] = sum(item["bytes"] or 0 for item in updated["items"] if item["action"] == "download")
                check["unknown_size_count"] = sum(item["bytes"] is None for item in updated["items"] if item["action"] == "download")
        error = ArtworkError("PLAN_STALE", "Local task or media observations changed; inspect the updated plan before choosing again.", "input")
        error.updated_plan = seal(updated)
        raise error


def admit_existing_plan(plan, output, metadata=None, lock_fd=None):
    """Check original state observations before RunState can recover anything.

    This does not add the picker-specific existing-lock requirement. Ordinary
    execution retains its locking policy; only the facts in the plan are bound.
    """
    expected = plan["local_observations"]
    try:
        with OutputDirectory(plan["destination"]["path"], create=False) as current:
            if _directory_identity(current) != expected["directory"] or _directory_identity(output) != expected["directory"]:
                raise ValueError()
            if metadata is not None:
                with current.child(STATE_DIRECTORY) as actual_metadata:
                    if _directory_identity(actual_metadata) != _directory_identity(metadata):
                        raise ValueError()
        saved = RunState.peek(output)
        if (saved is None or saved["sha256"] != expected["state_sha256"] or saved["revision"] != expected["state_revision"]
                or saved["payload"]["task_identity"] != plan["identity"]
                or saved["payload"]["task_fingerprint"] != plan["task_id"]):
            raise ValueError()
    except (ArtworkError, OSError, KeyError, TypeError, ValueError):
        raise ArtworkError("PLAN_STALE", "The planned task state or directory changed; create a fresh plan before execution.", "input") from None


@contextmanager
def open_existing_plan_output(plan):
    try:
        with OutputDirectory(plan["destination"]["path"], create=False) as output:
            admit_existing_plan(plan, output)
            yield output
    except FileNotFoundError:
        raise ArtworkError("PLAN_STALE", "The planned task directory no longer exists; create a fresh plan.", "input") from None


def _directory_identity(output):
    info = os.fstat(output._fd)
    return {"device": info.st_dev, "inode": info.st_ino}


def _busy(output):
    import fcntl
    try:
        with output.child(STATE_DIRECTORY) as metadata:
            lock = os.open("lock", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=metadata._fd)
            try:
                if not stat.S_ISREG(os.fstat(lock).st_mode):
                    raise ArtworkError("RUN_STATE_INVALID", "The task lock is not an ordinary file.", "download")
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
            finally:
                os.close(lock)
    except FileNotFoundError:
        pass
    return False


def observe(output, snapshot, layout_version, *, state=None, check_busy=True):
    """Capture actual hashes and stable file identities, without decoding/writing."""
    from .execution import _check_artifact, VALIDATION_POLICIES
    envelope = RunState.peek(output) if state is None else state
    data = envelope["payload"] if envelope else None
    if data is None and "legacy_files" in snapshot:
        data = {"files": snapshot["legacy_files"], "manifest": {"current": snapshot["legacy_manifest"]}, "manifest_pending": None}
    result = {"exists": True, "directory": _directory_identity(output),
              "state_sha256": envelope["sha256"] if envelope else None,
              "state_revision": envelope["revision"] if envelope else None,
              "busy": _busy(output) if check_busy else False, "manifest": None, "files": []}
    try:
        manifest = output.hash_file("manifest.json", max_bytes=MAX_JSON_BYTES)
        result["manifest"] = manifest
        if data and data.get("manifest_pending") is None:
            registered = data.get("manifest", {}).get("current")
            if not registered or any(manifest[key] != registered.get(key) for key in ("bytes", "sha256")):
                raise ArtworkError("MANIFEST_CONFLICT", "The manifest differs from the registered task result.", "download")
    except FileNotFoundError:
        if data and data.get("manifest", {}).get("current") is not None:
            raise ArtworkError("MANIFEST_CONFLICT", "The registered task manifest is missing.", "download") from None
    for plan in snapshot["files"]:
        record = (data or {}).get("files", {}).get(plan["item_id"], {})
        observed = {"item_id": plan["item_id"], "status": "missing"}
        directory = output
        child = None
        try:
            if layout_version == 2:
                child = directory.child("images" if plan["kind"] == "image" else "videos").__enter__()
                directory = child
            actual = directory.hash_file(plan["filename"], max_bytes=MAX_IMAGE_BYTES if plan["kind"] == "image" else MAX_MOTION_BYTES)
            observed.update(actual)
            if record.get("status") not in {"prepared", "committed", "conflict"} or record.get("artifact") is None:
                observed["status"] = "conflict"
            else:
                validation = _check_artifact(plan, record["artifact"])
                matches = all(actual[key] == validation[key] for key in ("bytes", "sha256"))
                observed["status"] = ("reusable" if record.get("validation_policy") == VALIDATION_POLICIES[plan["kind"]]
                                      else "validate_local") if matches else "conflict"
        except FileNotFoundError:
            pass
        except ArtworkError as error:
            if error.code == "RUN_STATE_INVALID":
                raise
            observed.update(status="conflict", error=error.to_dict())
        finally:
            if child is not None:
                child.__exit__(None, None, None)
        result["files"].append(observed)
    return result


def _destination(args, identity, name=None, allocation=None):
    resumed = getattr(args, "_library_allocation", None)
    if resumed is not None:
        return {"mode": "library", "path": os.path.abspath(args.output), "library_root": os.path.abspath(args._library_root),
                "version": resumed["version"], "allocation": resumed, "expected_allocation": resumed,
                "layout_version": 2, "resume_existing": True, "new_version": False}
    if args.output:
        return {"mode": "exact", "path": os.path.abspath(args.output), "library_root": None,
                "version": None, "allocation": None, "expected_allocation": None, "layout_version": 2,
                "derive_library_root": os.path.abspath("output"),
                "new_version": bool(getattr(args, "new_version", False))}
    root = os.path.abspath(getattr(args, "library", None) or "output")
    version = allocation["version"] + 1 if allocation and getattr(args, "new_version", False) else allocation["version"] if allocation else 1
    relative = proposed_path(root, identity, name, selection_label(args), allocation,
                             new_version=getattr(args, "new_version", False))
    return {"mode": "library", "path": str(Path(root) / relative), "library_root": root,
            "version": version, "allocation": allocation if not getattr(args, "new_version", False) else None,
            "expected_allocation": allocation, "layout_version": 2, "new_version": bool(getattr(args, "new_version", False))}


def preflight(args, *, http, environ, media_tools=None):
    """Fetch only declared metadata/HLS/HEAD; output paths remain untouched."""
    from .execution import (_task_identity, _check_options, _new_manifest, _asset_value,
                            _selector, _make_plan, _inspect_motion_assets, _validate_base)
    target = parse_url(args.url)
    _check_options(args, target)
    if args.output and getattr(args, "library", None):
        raise ArtworkError("INVALID_ARGUMENTS", "--output and --library are mutually exclusive.", "input")
    payload = load_json(args.response) if args.response and not getattr(args, "_resume_identity", None) else None
    identity = deepcopy(getattr(args, "_resume_identity", None)) or _task_identity(args, target, payload)
    task_id = fingerprint(identity)
    allocation = None if args.output else resolve(getattr(args, "library", None) or "output", task_id)
    destination = _destination(args, identity, allocation=allocation)
    saved, legacy, inspected_motion, startup_checks = None, None, False, []
    try:
        with OutputDirectory(destination["path"], create=False) as output:
            if getattr(args, "new_version", False) and args.output and output.names():
                raise ArtworkError("NEW_OUTPUT_REQUIRED", "A new version with --output requires a new explicit directory.", "input")
            if _busy(output):
                raise ArtworkError("OUTPUT_BUSY", "Another process is using this task directory; wait for it to finish.", "download")
            saved = RunState.peek(output)
            if saved is None:
                try:
                    legacy = output.read_file("manifest.json", max_bytes=MAX_JSON_BYTES)[0]
                except FileNotFoundError:
                    pass
            directory_exists = True
    except FileNotFoundError:
        directory_exists = False
    if saved is not None:
        data = saved["payload"]
        if data.get("task_fingerprint") != task_id or data.get("task_identity") != identity:
            raise ArtworkError("TASK_CONFLICT", "This directory belongs to different task parameters; select a new destination.", "download")
        snapshot = deepcopy(data["snapshot"])
        _validate_base(snapshot["manifest"], args, target)
        plans, issues = _make_plan(snapshot["manifest"], snapshot["files"])
        if plans != snapshot["files"] or set(data["files"]) != {plan["item_id"] for plan in plans}:
            raise ArtworkError("RUN_STATE_INVALID", "The frozen file plan cannot be reconstructed.", "download")
        destination["layout_version"] = data.get("layout_version", 1)
        if type(destination["layout_version"]) is not int or destination["layout_version"] not in {1, 2}:
            raise ArtworkError("RUN_STATE_INVALID", "Unknown media layout version.", "download")
    elif legacy is not None:
        from .execution import _check_artifact
        try:
            old = loads_json(legacy)
            if args.response or old.get("provider") not in {"web", "catalog"} or "run" in old:
                raise ValueError()
            _validate_base(old, args, target)
            manifest = deepcopy(old)
            for name in ("manifest_report", "status_scope", "download_status"):
                manifest.pop(name, None)
            manifest.update(downloads=[], errors=[], status="success")
            plans, issues = _make_plan(manifest)
            by_path = {plan["filename"]: plan for plan in plans}
            records = {}
            for item in old["downloads"]:
                if item.get("status") != "downloaded":
                    continue
                plan = by_path.pop(item["path"])
                _check_artifact(plan, item)
                if item.get("references") != plan["references"]:
                    raise ValueError()
                records[plan["item_id"]] = {"status": "committed", "artifact": item,
                                             "validation_policy": "legacy-unverified"}
            snapshot = {"created_at": None, "manifest": manifest, "files": plans, "legacy_files": records,
                        "legacy_manifest": {"bytes": len(legacy), "sha256": sha256(legacy).hexdigest()}}
            destination["layout_version"] = 1
        except (ArtworkError, KeyError, TypeError, ValueError):
            raise ArtworkError("LEGACY_MANIFEST_UNRECOGNIZED", "The old manifest cannot reconstruct this task safely.", "download") from None
    else:
        client = AppleMusicClient(http, provider=args.provider,
                                  developer_token=environ.get(args.token_env) if args.provider == "catalog" else None)
        source_manifest = getattr(args, "_source_manifest", None)
        if source_manifest is not None:
            from .execution import _options
            manifest = deepcopy(source_manifest)
            manifest.update(options=_options(args), selection=None, downloads=[], errors=[], motion_inspections=[], status="success")
        else:
            manifest = _new_manifest(args, target, client, payload)
        assets = tuple(_asset_value(value) for value in manifest["assets"])
        evidence = manifest["source_evidence"]
        source = SourceEvidence(evidence["requested_storefront"], evidence["requested_locale"],
                                tuple(StorefrontObservation(**{**item, "storefront": item["storefront"] or ""})
                                      for item in evidence["observations"]))
        try:
            manifest["selection"] = select(DiscoveryResult(target, assets, source_evidence=source), _selector(args)).to_dict()
        except ArtworkError as error:
            error.manifest = manifest
            raise
        videos = [_asset_value(value) for value in manifest["selection"]["selected"] if value["kind"] == "video"]
        dependency_error = None
        if videos:
            try:
                from .hls import require_parser
                require_parser()
                if media_tools is None:
                    from .motion import MediaTools
                    MediaTools.discover(ffmpeg=environ.get("APPLE_ARTWORK_FFMPEG"), ffprobe=environ.get("APPLE_ARTWORK_FFPROBE"), control=http.control)
                startup_checks.append({"id": "motion_dependencies", "status": "passed"})
            except ArtworkError as error:
                dependency_error = error
                startup_checks.append({"id": "motion_dependencies", "status": "blocked", "startup_blocker": True, "error": error.to_dict()})
        if dependency_error is None:
            inspected_motion = bool(videos)
            _inspect_motion_assets(videos, manifest, http, args.motion_profile)
        else:
            manifest["motion_inspections"] = [{"asset_id": asset.asset_id, "semantic_path": asset.semantic_path,
                "references": [asset.to_dict()], "status": "failed", "error": dependency_error.to_dict()} for asset in videos]
        plans, issues = _make_plan(manifest)
        snapshot = {"created_at": getattr(args, "_source_created_at", utc_now()), "manifest": manifest, "files": plans}
        if not args.output:
            destination = _destination(args, identity, manifest.get("resource_name"), allocation)
        if directory_exists:
            with OutputDirectory(destination["path"], create=False) as output:
                if "manifest.json" in output.names():
                    destination["layout_version"] = 1
    try:
        with OutputDirectory(destination["path"], create=False) as output:
            observations = observe(output, snapshot, destination["layout_version"], state=saved)
    except FileNotFoundError:
        observations = {"exists": False, "directory": None, "state_sha256": None,
                        "state_revision": None, "busy": False, "manifest": None,
                        "files": [{"item_id": plan["item_id"], "status": "missing"} for plan in plans]}
    known = {entry["item_id"]: entry for entry in observations["files"]}
    items = []
    for index, plan in enumerate(plans, 1):
        local = known[plan["item_id"]]
        action = {"reusable": "reuse", "validate_local": "validate_local", "conflict": "needs_action"}.get(local["status"], "download")
        record = saved["payload"]["files"][plan["item_id"]] if saved else {}
        if record.get("status") == "skipped":
            action = "skip"
        if action == "download" and record.get("status") in {"failed", "conflict"} and record.get("error"):
            action = "needs_action"
        request = plan["request"]
        selected = request.get("selected_variant", request.get("selected", {}))
        item = {"item_id": plan["item_id"], "name": ("图片 " if plan["kind"] == "image" else "视频 ") + str(index),
                "kind": plan["kind"], "name_key": plan["asset"]["semantic_path"], "action": action, "path": ("images/" if plan["kind"] == "image" else "videos/") + plan["filename"]
                    if destination["layout_version"] == 2 else plan["filename"],
                "bytes": local.get("bytes") or (plan["request"].get("layout") or {}).get("content_length"),
                "spec": {"width": request.get("width", selected.get("width")), "height": request.get("height", selected.get("height")),
                         "format": request.get("format", "mp4" if plan["kind"] == "video" else None),
                         "codec": selected.get("codec"), "frame_rate": selected.get("frame_rate"),
                         "duration_seconds": (request.get("layout") or {}).get("duration")}}
        if action == "needs_action":
            item["error"] = local.get("error") or record.get("error") or ArtworkError("OUTPUT_CONFLICT", "Local media differs from the verified record; it will be preserved.", "download").to_dict()
        if plan.get("blocked_error") and action != "skip":
            item.update(action="needs_action", error=plan["blocked_error"])
        items.append(item)
    for issue in issues:
        skipped = saved and "issue_" + issue["asset_id"] in saved["payload"].get("skipped_issues", [])
        items.append({"item_id": "issue_" + issue["asset_id"], "name": "未解决素材", "kind": issue["kind"],
                      "action": "skip" if skipped else "needs_action", "path": None, "bytes": None, "error": issue["error"]})
    checks = [*startup_checks, {"id": "output_busy", "status": "blocked" if observations["busy"] else "passed"}]
    required_videos = [item for item in items if item["kind"] == "video" and item["action"] in {"download", "validate_local"}]
    if required_videos and not startup_checks:
        try:
            if media_tools is None:
                from .motion import MediaTools
                MediaTools.discover(ffmpeg=environ.get("APPLE_ARTWORK_FFMPEG"), ffprobe=environ.get("APPLE_ARTWORK_FFPROBE"), control=http.control)
            checks.append({"id": "motion_tools", "status": "passed"})
        except ArtworkError as error:
            checks.append({"id": "motion_tools", "status": "blocked", "startup_blocker": True, "error": error.to_dict()})
            for item in required_videos:
                item.update(action="needs_action", error=error.to_dict())
    # Space and permission observations use an existing ancestor only; no probe
    # file or target directory is created by preflight.
    ancestor = Path(destination["path"])
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    with OutputDirectory(ancestor, create=False) as parent:
        free = os.fstatvfs(parent._fd)
        available = free.f_bavail * free.f_frsize
        checks.extend([{"id": "space", "status": "estimated", "available_bytes": available,
                        "known_required_bytes": sum(item["bytes"] or 0 for item in items if item["action"] == "download"),
                        "unknown_size_count": sum(item["bytes"] is None for item in items if item["action"] == "download")},
                       {"id": "permissions", "status": "estimated", "writable": os.access(parent._absolute, os.W_OK)}])
    plan = {"protocol_version": 1, "task_id": task_id, "identity": identity, "target": target.to_dict(),
            "destination": destination, "canonical_options": identity["options"], "snapshot": snapshot,
            "local_observations": observations, "items": items, "checks": checks,
            "network_scope": {"resource_metadata": not bool(args.response) and saved is None and legacy is None and not getattr(args, "_source_manifest", None),
                              "hls_and_head": inspected_motion,
                              "media_bodies": False}, "plan_id": None, "decisions": []}
    return seal(plan)
