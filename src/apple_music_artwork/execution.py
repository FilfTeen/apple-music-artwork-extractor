"""One coordinator owns the frozen plan, persistence, and progress output."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import math
from pathlib import Path
import os
import re
import signal
from threading import Semaphore, current_thread, main_thread
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from .client import AppleMusicClient, HttpClient, MAX_IMAGE_BYTES, MAX_MOTION_BYTES, validate_url
from .discovery import discover
from .download import OutputCommitError, OutputDirectory, prepare_image, output_filename, validate_image
from .errors import ArtworkError
from .jsonio import MAX_JSON_BYTES, dumps_json, load_json, loads_json
from .models import (ImageOptions, ImageRequest, SelectionRequest, ResourceKey, Target, VisualAsset,
                     Observation, VisualDeclaration, StorefrontObservation, SourceEvidence, DiscoveryResult)
from .rendition import resolve_image, source_identity
from .run_state import (RunState, fingerprint, utc_now, json_bytes, valid_temporary_record, residue_paths)
from .selection import select
from .urls import parse_url


EXIT_CODES = {"internal": 1, "input": 2, "auth": 3, "api": 4,
              "discovery": 5, "selection": 5, "rendition": 6, "download": 6, "validation": 7}
VALIDATION_POLICIES = {"image": "image-decode-v1", "video": "mp4-packet-full-decode-v1"}


def _error_result(error: ArtworkError) -> dict:
    return {"schema_version": 2, "status": "failed", "errors": [error.to_dict()], "downloads": []}


def _status(manifest: dict) -> int:
    errors = manifest["errors"]
    if errors:
        successful = any(item["status"] == "downloaded" for item in manifest["downloads"])
        successful |= manifest.get("mode") == "list" and any(
            item["status"] == "supported" for item in manifest.get("motion_inspections", []))
        successful |= any(item["status"] in {"success", "partial"}
                          for item in manifest.get("comparison", {}).get("results", []))
        manifest["status"] = "partial" if successful else "failed"
        return EXIT_CODES.get(errors[0]["category"], 1)
    manifest["status"] = "success"
    return 0


def _check_options(args, target):
    if args.list and (args.semantic or args.asset_id or args.all_static or args.all_media or args.all_motion):
        raise ArtworkError("INVALID_ARGUMENTS", "Selection flags require download mode; --list inspects every visual.", "input")
    if args.inspect_motion and not args.list:
        raise ArtworkError("INVALID_ARGUMENTS", "--inspect-motion requires --list.", "input")
    if args.compare_storefronts:
        if not args.list or args.response:
            raise ArtworkError("INVALID_ARGUMENTS", "Storefront comparison requires online --list mode.", "input")
        if target.locale is None:
            raise ArtworkError("COMPARISON_LOCALE_REQUIRED", "Storefront comparison requires an explicit l language parameter in the input URL.", "input")
        storefronts = list(dict.fromkeys([target.key.storefront, *args.compare_storefronts]))
        if len(storefronts) > 8 or any(not re.fullmatch(r"[a-z]{2}", value) for value in storefronts):
            raise ArtworkError("INVALID_ARGUMENTS", "Compare at most eight lowercase two-letter storefronts.", "input")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", args.token_env):
        raise ArtworkError("INVALID_ARGUMENTS", "Token environment variable name is invalid.", "input")


def _coverage(manifest):
    """Account for observed work without equating a successful command with completeness."""
    coverage = manifest.get("coverage")
    if coverage is None:
        return
    assets = manifest.get("assets", [])
    selected = {asset["asset_id"] for asset in (manifest.get("selection") or {}).get("selected", [])}
    inspected = {}
    for entry in manifest.get("motion_inspections", []):
        for reference in entry.get("references", []):
            inspected[reference["asset_id"]] = entry["status"]
    downloaded, validated, failed, skipped = set(), set(), set(), set()
    for entry in manifest["downloads"]:
        references = {reference["asset_id"] for reference in entry.get("references", [])}
        references.add(entry["asset_id"])
        if entry["status"] == "downloaded":
            downloaded.update(references)
            if entry.get("validation", {}).get("decoded") is True:
                validated.update(references)
        elif entry["status"] == "skipped":
            skipped.update(references)
        else:
            failed.update(references)
    coverage["inspection"] = {
        "motion": [{"asset_id": asset["asset_id"], "status": inspected.get(asset["asset_id"], "uninspected")}
                   for asset in assets if asset["kind"] == "video"],
    }
    coverage["download"] = {
        "selected_asset_ids": sorted(selected), "downloaded_asset_ids": sorted(downloaded),
        "failed_asset_ids": sorted(failed),
        "unselected_asset_ids": sorted(asset["asset_id"] for asset in assets if asset["asset_id"] not in selected),
    }
    if skipped:
        coverage["download"]["skipped_asset_ids"] = sorted(skipped)
    coverage["validation"] = {
        "validated_asset_ids": sorted(validated),
        "unvalidated_asset_ids": sorted(asset["asset_id"] for asset in assets if asset["asset_id"] not in validated),
    }
    unresolved = []
    if manifest.get("source_evidence", {}).get("status") != "verified":
        unresolved.append("source_storefront_unverified")
    if coverage.get("scan", {}).get("status") != "completed":
        unresolved.append("candidate_scan_incomplete")
    if not coverage.get("request_observations") or any(not item.get("known") for item in coverage["request_observations"]):
        unresolved.append("request_coverage_unknown")
    for name, field in coverage.get("fields", {}).items():
        if field.get("state") not in {"present", "null", "empty"}:
            unresolved.append("field_" + name + "_" + field.get("state", "unknown"))
    if coverage.get("unknown_candidate_count", 0) or any(asset["role"] == "unknown" for asset in assets):
        unresolved.append("unknown_candidates")
    if any(asset["kind"] == "video" and inspected.get(asset["asset_id"]) != "supported" for asset in assets):
        unresolved.append("motion_not_fully_inspected_or_unsupported")
    if any(asset["asset_id"] not in validated for asset in assets):
        unresolved.append("assets_not_fully_downloaded_and_validated")
    if manifest["errors"]:
        unresolved.append("execution_errors")
    if skipped:
        unresolved.append("explicitly_skipped_assets")
    coverage["completeness"] = {
        "scope": "resource_storefront_locale_returned_fields_and_supported_layouts",
        "status": "complete" if not unresolved else "incomplete",
        "unresolved": sorted(set(unresolved)),
    }


def _inspect_motion_assets(assets, manifest, http, profile, call=None):
    from .hls import inspect_motion

    groups = {}
    for asset in assets:
        if asset.kind == "video":
            groups.setdefault(asset.url, []).append(asset)
    inspections = {}
    manifest.setdefault("motion_inspections", [])
    for references in groups.values():
        asset = references[0]
        entry = {"asset_id": asset.asset_id, "semantic_path": asset.semantic_path,
                 "references": [reference.to_dict() for reference in references]}
        try:
            http.control.emit({"kind": "stage", "stage": "inspect", "item_id": None,
                               "semantic_path": asset.semantic_path})
            operation = lambda: inspect_motion(asset, http, profile=profile)
            inspection = call(operation) if call is not None else operation()
            entry.update(status=inspection.status, inspection=inspection.to_dict())
            for reference in references:
                inspections[reference.asset_id] = inspection
            if inspection.error is not None:
                entry["error"] = inspection.error.to_dict()
                manifest["errors"].append(inspection.error.to_dict())
        except ArtworkError as error:
            entry.update(status="failed", error=error.to_dict())
            partial = getattr(error, "inspection", None)
            if partial is not None:
                entry["inspection"] = partial.to_dict()
            manifest["errors"].append(error.to_dict())
        manifest["motion_inspections"].append(entry)
        if http.control.cancelled:
            break
    return inspections


def _failed_download(asset, error, *, references=None):
    return {"asset_id": asset.asset_id, "semantic_path": asset.semantic_path, "kind": asset.kind,
            "status": "failed", "error": error.to_dict(),
            "references": references if references is not None else [asset.to_dict()]}


def _skip_item(item):
    """Keep the original controlled reason outside current unresolved errors."""
    item["status"] = "skipped"
    reason = item.pop("error", None)
    if reason is not None:
        item["skip_reason"] = reason


def _motion_constraints(inspection):
    selected = inspection.selected
    return {"width": selected.width, "height": selected.height, "codecs": selected.codecs.strip(),
            "frame_rate": selected.frame_rate, "duration_seconds": inspection.layout.duration}


def _same_motion_constraints(first, other):
    if any(first[key] != other[key] for key in ("width", "height", "codecs")):
        return False
    # Only floating-point representation noise is equivalent, not differing durations or frame rates.
    return all(math.isclose(first[key], other[key], rel_tol=1e-12, abs_tol=1e-9)
               for key in ("frame_rate", "duration_seconds"))


def _comparison_signature(asset):
    identity = {key: asset.get(key) for key in ("semantic_path", "kind", "role", "url", "width", "height", "traits")}
    identity["declarations"] = sorted(
        ({"name": item["name"], "value": item["value"]} for item in asset.get("declarations", [])),
        key=dumps_json,
    )
    return dumps_json(identity)


def _invalid():
    return ArtworkError("RUN_STATE_INVALID", "Task state cannot be reconstructed safely; use a new output directory.", "download")


def _internal():
    return ArtworkError("INTERNAL_ERROR", "Unexpected internal failure; no diagnostic response data was emitted.", "internal")


def _error_value(value):
    if (not isinstance(value, dict) or not all(isinstance(value.get(key), str) and value[key]
                                              for key in ("code", "message", "category"))
            or type(value.get("retryable")) is not bool or value["category"] not in EXIT_CODES):
        raise _invalid()
    if value["code"] == "RETRY_AFTER_DEADLINE":
        from .runtime import RetryAfterDeadline
        if any(type(value.get(key)) not in {int, float} or not math.isfinite(value[key]) or value[key] < 0
               for key in ("retry_after_seconds", "retry_at")):
            raise _invalid()
        return RetryAfterDeadline(value["retry_after_seconds"], value["retry_at"], value["category"])
    try:
        return ArtworkError(value["code"], value["message"], value["category"], value["retryable"], context=value.get("context"))
    except ValueError:
        raise _invalid() from None


def _asset_value(value, owner=None):
    """Persisted fields are data, never a route around current domain identity."""
    try:
        if not isinstance(value, dict):
            raise _invalid()
        key = ResourceKey(**value["owner"])
        if owner is not None and key != owner:
            raise _invalid()
        if (not all(isinstance(value.get(name), str) and value[name]
                    for name in ("semantic_path", "kind", "role", "url", "source_identity"))
                or value["kind"] not in {"image", "video", "unknown"}
                or not isinstance(value.get("traits"), list)
                or any(not isinstance(item, str) for item in value["traits"])
                or any(value.get(name) is not None and (type(value[name]) is not int or value[name] <= 0)
                       for name in ("width", "height"))):
            raise _invalid()
        observations = tuple(Observation(**item) for item in value["observations"])
        declarations = tuple(VisualDeclaration(item["name"], item["value"],
                             tuple(Observation(**observation) for observation in item["observations"]))
                             for item in value["declarations"])
        asset = VisualAsset(key, value["semantic_path"], value["kind"], value["role"], value["url"],
                            value["source_identity"], value["width"], value["height"],
                            tuple(value["traits"]), observations, declarations)
        if asset.to_dict() != value or asset.source_identity != source_identity(asset.url, asset.kind):
            raise _invalid()
        return asset
    except (KeyError, TypeError, ValueError, AttributeError):
        raise _invalid() from None


def _inspection_value(value, asset):
    """Restore a completed immutable HLS request, with no parser or HTTP call."""
    from .hls import MotionInspection, MotionLayout, MotionVariant, _select

    def variant(item):
        copied = dict(item)
        codec = copied.pop("codec")
        result = MotionVariant(**copied)
        if (result.codec != codec or result.codec not in {"h264", "hevc"}
                or any(type(number) is not int or number <= 0 for number in (result.width, result.height))
                or type(result.frame_rate) not in {int, float} or not math.isfinite(result.frame_rate)
                or result.frame_rate <= 0):
            raise _invalid()
        validate_url(result.url, "hls")
        return result

    try:
        if (not isinstance(value, dict) or value.get("asset_id") != asset.asset_id
                or value.get("master_url") != asset.url or value.get("status") != "supported"
                or value.get("error") is not None or value.get("profile") not in {"best", "compatible"}
                or value.get("media_body_checked") is not False or value.get("provisional_layout") is not None):
            raise _invalid()
        validate_url(value["master_url"], "hls")
        validate_url(value["resolved_master_url"], "hls")
        selected = variant(value["selected"])
        # Other advertised variants may be unsupported. Only the frozen choice
        # must satisfy the downloadable-video constraints.
        variants = tuple(MotionVariant(**{key: item for key, item in record.items() if key != "codec"})
                         for record in value["variants"])
        if selected not in variants or _select(variants, value["profile"]) != selected:
            raise _invalid()
        layout = value["layout"]
        if (layout.get("kind") != "complete_single_mp4" or layout.get("ended") is not True
                or layout.get("encrypted") is not False or layout.get("whole_file_coverage") is not True
                or type(layout.get("content_length")) is not int
                or not 0 < layout["content_length"] <= MAX_MOTION_BYTES
                or type(layout.get("duration")) not in {int, float} or not math.isfinite(layout["duration"])
                or layout["duration"] <= 0 or type(layout.get("segment_count")) is not int
                or not 0 < layout["segment_count"] <= 1024):
            raise _invalid()
        for name, purpose in (("mp4_url", "motion"), ("source_mp4_url", "motion"), ("media_url", "hls")):
            validate_url(layout[name], purpose)
        ranges = tuple((item["offset"], item["length"]) for item in layout["ranges"])
        end = 0
        for offset, length in ranges:
            if type(offset) is not int or type(length) is not int or offset != end or length <= 0:
                raise _invalid()
            end += length
        if end != layout["content_length"] or len(ranges) != layout["segment_count"] + 1:
            raise _invalid()
        restored = MotionLayout(layout["mp4_url"], layout["source_mp4_url"], layout["media_url"],
                                layout["content_length"], layout["duration"], layout["segment_count"], ranges)
        return MotionInspection(asset.asset_id, asset.url, value["profile"], "supported", variants,
                                selected, restored, None, value["resolved_master_url"], value["iframe_variant_count"])
    except ArtworkError:
        raise _invalid() from None
    except (KeyError, TypeError, ValueError, AttributeError):
        raise _invalid() from None


def _options(args):
    return {"width": args.width, "height": args.height,
            "format": args.format or ("png" if args.all_media else "jpg"), "crop": "bb",
            "motion_profile": args.motion_profile}


def _selector(args):
    return SelectionRequest(args.semantic, args.asset_id, args.all_static, args.all_media, args.all_motion)


def _task_identity(args, target, payload):
    return {"resource": target.key.to_dict(), "locale": target.locale,
            "provider": "offline" if args.response else args.provider,
            "source": {"kind": "offline", "content_sha256": fingerprint(payload)} if args.response else {"kind": "online"},
            "selection": {"semantic_path": args.semantic, "asset_id": args.asset_id,
                          "all_static": args.all_static, "all_media": args.all_media, "all_motion": args.all_motion},
            "options": _options(args)}


def _new_manifest(args, target, client, payload, call=None):
    if args.response:
        variant, fields = "import", ()
    else:
        payload = call(lambda: client.fetch(target)) if call is not None else client.fetch(target)
        variant = "typed-extend-web" if args.provider == "web" else "catalog"
        fields = ("artwork", "editorialArtwork", "editorialVideo") if args.provider == "web" else ("artwork",)
    result = discover(payload, target, variant=variant, requested_fields=fields)
    manifest = {**result.to_dict(), "mode": "list" if args.list else "download",
                "provider": "offline" if args.response else args.provider, "status": "success",
                "selection": None, "downloads": [], "errors": [], "motion_inspections": []}
    if not args.list:
        manifest["options"] = _options(args)
    return manifest


def _validate_base(manifest, args, target):
    try:
        saved_target = manifest["target"]
        restored_target = parse_url(saved_target["input_url"])
        if (restored_target.to_dict() != saved_target or restored_target.key != target.key
                or restored_target.locale != target.locale or manifest["schema_version"] != 2
                or manifest["mode"] != "download" or manifest["options"] != _options(args)
                or manifest["provider"] != ("offline" if args.response else args.provider)):
            raise _invalid()
        assets = tuple(_asset_value(value, target.key) for value in manifest["assets"])
        if len({asset.asset_id for asset in assets}) != len(assets):
            raise _invalid()
        evidence = manifest["source_evidence"]
        observations = tuple(StorefrontObservation(**{**item, "storefront": item["storefront"] or ""})
                             for item in evidence["observations"])
        source = SourceEvidence(evidence["requested_storefront"], evidence["requested_locale"], observations)
        if (source.to_dict() != evidence or source.requested_storefront != target.key.storefront
                or source.requested_locale != target.locale or source.status == "conflict"):
            raise _invalid()
        result = DiscoveryResult(restored_target, assets, source_evidence=source)
        if select(result, _selector(args)).to_dict() != manifest["selection"]:
            raise _invalid()
        if not isinstance(manifest["motion_inspections"], list) or not isinstance(manifest["coverage"], dict):
            raise _invalid()
        return assets
    except ArtworkError as error:
        if error.code == "RUN_STATE_INVALID":
            raise
        raise _invalid() from None
    except (KeyError, TypeError, ValueError, AttributeError):
        raise _invalid() from None


def _make_plan(manifest, previous=()):
    options = ImageOptions(**{key: manifest["options"][key] for key in ("width", "height", "format", "crop")})
    inspections, groups, issues, requests = {}, {}, [], {}
    for entry in manifest["motion_inspections"]:
        for reference in entry["references"]:
            inspections[reference["asset_id"]] = entry
    for ordinal, value in enumerate(manifest["selection"]["selected"], 1):
        asset = _asset_value(value)
        try:
            if asset.kind == "video":
                entry = inspections.get(asset.asset_id)
                if entry is None or entry["status"] == "pending":
                    continue
                if entry["status"] != "supported":
                    issues.append(_failed_download(asset, _error_value(entry["error"])))
                    continue
                original = _asset_value(entry["references"][0])
                request = _inspection_value(entry["inspection"], original)
                from .motion import motion_filename
                filename = motion_filename(asset, request)
                key = ["video", *request.request_key]
                request_value = request.to_dict()
            else:
                request = resolve_image(asset, options)
                key = ["image", request.url, request.width, request.height, request.format]
                filename, request_value = output_filename(asset, request), request.to_dict()
            item_id = "file_" + fingerprint(key)
            requests[asset.asset_id] = (value, request_value, filename)
            if item_id not in groups:
                groups[item_id] = {"item_id": item_id, "ordinal": ordinal, "kind": asset.kind,
                                   "asset": value, "request": request_value, "filename": filename,
                                   "references": [], "motion_declarations": [], "blocked_error": None}
            group = groups[item_id]
            group["references"].append(value)
            if asset.kind == "video":
                group["motion_declarations"].append({"asset_id": asset.asset_id, **_motion_constraints(request)})
        except ArtworkError as error:
            if error.code == "RUN_STATE_INVALID":
                raise
            issues.append(_failed_download(asset, error))
    old_groups = {item["item_id"]: item for item in previous}
    for group in groups.values():
        old = old_groups.get(group["item_id"])
        if old is not None:
            try:
                anchor = requests[old["asset"]["asset_id"]]
                if (old["asset"] not in group["references"] or anchor != (old["asset"], old["request"], old["filename"])):
                    raise _invalid()
                group.update(asset=old["asset"], request=old["request"], filename=old["filename"])
            except (KeyError, TypeError):
                raise _invalid() from None
        declarations = group["motion_declarations"]
        if declarations and any(not _same_motion_constraints(declarations[0], other) for other in declarations[1:]):
            group["blocked_error"] = ArtworkError("MOTION_DECLARATION_CONFLICT", "Aliases of the same MP4 request disagree on video dimensions, codec, frame rate or duration.", "rendition").to_dict()
    return list(groups.values()), issues


def _request_value(plan):
    if plan["kind"] == "image":
        return ImageRequest(**plan["request"])
    # The request retains the first master observation, even if an alias is the
    # first selected semantic reference in a later reconstruction.
    asset = next(_asset_value(value) for value in plan["references"]
                 if value["asset_id"] == plan["request"]["asset_id"])
    return _inspection_value(plan["request"], asset)


def _declared_p3(plan):
    return tuple(declaration["value"] for value in plan["references"]
                 for declaration in value["declarations"] if declaration["name"] == "hasP3")


def _check_artifact(plan, artifact):
    """Validate the recorded proof against this exact frozen physical request."""
    try:
        request, asset = _request_value(plan), _asset_value(plan["asset"])
        validation = artifact["validation"]
        limit = MAX_IMAGE_BYTES if plan["kind"] == "image" else MAX_MOTION_BYTES
        if (artifact.get("status") != "downloaded" or artifact.get("asset_id") != asset.asset_id
                or artifact.get("kind", plan["kind"]) != plan["kind"] or artifact.get("path") != plan["filename"]
                or validation.get("decoded") is not True or type(validation.get("bytes")) is not int
                or not 0 < validation["bytes"] <= limit or not isinstance(validation.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", validation["sha256"]) is None):
            raise _invalid()
        if plan["kind"] == "image":
            dimensions = {"width": request.width, "height": request.height}
            if (artifact.get("url") != request.url or artifact.get("requested_dimensions") != dimensions
                    or artifact.get("requested_format") != request.format
                    or validation.get("actual_dimensions") != dimensions
                    or validation.get("actual_format") != {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "avif": "AVIF"}.get(request.format)):
                raise _invalid()
        else:
            dimensions = {"width": request.selected.width, "height": request.selected.height}
            if (artifact.get("layout") != request.layout.to_dict()
                    or artifact.get("selected_variant") != request.selected.to_dict()
                    or artifact.get("profile") != request.profile or validation.get("full_decode") is not True
                    or validation.get("bytes") != request.layout.content_length
                    or validation.get("actual_dimensions") != dimensions
                    or validation.get("actual_format") != "MP4" or validation.get("actual_codec") != request.selected.codec):
                raise _invalid()
            validate_url(artifact["url"], "motion")
        return validation
    except (KeyError, TypeError, ValueError, AttributeError):
        raise _invalid() from None


class _Cancellation:
    def __init__(self, control):
        self.control, self.user_cancelled, self.previous = control, False, None
        self.woken = False

    def cancel(self, *_):
        # A Python signal can interrupt a coordinator while a runtime/Event
        # lock is held. The handler must only assign lock-free request flags.
        self.user_cancelled = True
        self.control.request_cancel()

    def poll(self):
        if self.user_cancelled and not self.woken:
            self.control.cancel()
            self.woken = True

    def __enter__(self):
        if current_thread() is main_thread():
            self.previous = signal.signal(signal.SIGINT, self.cancel)
        return self

    def __exit__(self, *_):
        if self.previous is not None:
            signal.signal(signal.SIGINT, self.previous)


class _ValidationGate:
    def __init__(self, control):
        self.control, self.semaphore = control, Semaphore(1)

    def __enter__(self):
        while not self.semaphore.acquire(timeout=0.1):
            self.control.check(category="validation")
        try:
            self.control.check(category="validation")
        except BaseException:
            self.semaphore.release()
            raise

    def __exit__(self, *_):
        self.semaphore.release()


def _serial_call(control, drain, operation):
    """Keep the coordinator responsive while one serial network call waits.

    Discovery/credential calls never overlap; this slot is joined before the
    next call or before file workers start. Only the caller drains UI events.
    """
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="apple-artwork-metadata") as executor:
        future = executor.submit(operation)
        try:
            while not future.done():
                drain()
                wait((future,), timeout=0.05)
            drain()
            return future.result()
        except BaseException:
            if not future.done():
                control.cancel()
            raise


class _Coordinator:
    def __init__(self, args, target, identity, output, state, http, environ, media_tools, event_sink, cancellation,
                 *, plan=None, decision_provider=None, choices=()):
        self.args, self.target, self.identity = args, target, identity
        self.output, self.state, self.http = output, state, http
        self.environ, self.media_tools, self.event_sink = environ, media_tools, event_sink
        self.control, self.cancellation = http.control, cancellation
        self.gate, self.plans, self.issues, self.outcomes = _ValidationGate(self.control), [], [], {}
        self.errors, self.fatal, self.resumed = [], False, False
        self.prepared_plan, self.decision_provider = plan, decision_provider
        self.v04 = plan is not None
        self.choices = {item["decision_id"]: item for item in choices}
        self.layout_version = plan["destination"]["layout_version"] if plan else 1
        self.resources, self.directories = ExitStack(), {}
        self.journal = None
        self.journal_warnings = []
        self.sequence, self.progress = 0, {}
        self.active_count, self.queued_count = 0, 0
        self.active_items = set()
        self.lifecycle, self.decisions, self.control_exit = "running", [], None
        self.next_task = None
        self.current_plan_id = plan["plan_id"] if plan else None
        self.manifest = {"schema_version": 2, "mode": "download", "target": target.to_dict(),
                         "downloads": [], "errors": [], "status": "failed"}
        self.run = {"run_id": getattr(event_sink, "run_id", None) or str(uuid4()), "task_fingerprint": fingerprint(identity), "resumed": False,
                    "snapshot": {"created_at": None, "remote_freshness": "not_rechecked"}, "jobs": args.jobs,
                    "counts": {}, "stats": {}, "cancelled": False,
                    "pending_inspection": {"attempted": 0, "completed": 0, "checked_at": None}}

    def media(self, plan):
        if self.layout_version == 1:
            return self.output
        kind = "images" if plan["kind"] == "image" else "videos"
        if kind not in self.directories:
            self.directories[kind] = self.resources.enter_context(self.output.child(kind, create=True))
        return self.directories[kind]

    def public_path(self, plan):
        prefix = ("images/" if plan["kind"] == "image" else "videos/") if self.layout_version == 2 else ""
        return prefix + plan["filename"]

    def send_event(self, event):
        if self.v04:
            self.sequence += 1
            event = {**event, "schema_version": 1, "sequence": self.sequence,
                     "run_id": self.run["run_id"], "task_id": self.run["task_fingerprint"],
                     "plan_id": self.current_plan_id}
        prepare = getattr(self.event_sink, "prepare", None)
        if callable(prepare):
            event = prepare(event)
        if self.journal is not None:
            self.journal.emit(event)
        if callable(prepare):
            self.event_sink.deliver(event)
        elif self.event_sink is not None:
            self.event_sink(event)

    def progress_snapshot(self, active, queued):
        stages = {"queued": "pending", "download": "downloading", "waiting_validation": "waiting_validation",
                  "validate": "validating", "save": "saving", "committed": "completed", "reused": "reused",
                  "failed": "failed", "skipped": "skipped", "cancelled": "cancelled", "not_started": "not_started"}
        counts = {name: 0 for name in stages.values()}
        items = []
        for ordinal, plan in enumerate(self.plans, 1):
            item_id = plan["item_id"]
            current = self.progress.get(item_id, {})
            outcome = self.outcomes.get(item_id)
            stage = current.get("stage", "queued")
            if outcome:
                stage = ("reused" if outcome.get("action") == "reused" else "committed" if outcome["status"] == "downloaded"
                         else outcome["status"] if outcome["status"] in {"skipped", "cancelled", "not_started"} else "failed")
            counts[stages.get(stage, "pending")] += 1
            if outcome is None and (item_id in self.active_items or stage in {"download", "waiting_validation", "validate", "save"}):
                request = plan["request"]
                selected = request.get("selected") or {}
                items.append({"item_id": item_id, "name": ("图片 " if plan["kind"] == "image" else "视频 ") + str(ordinal),
                              "name_key": plan["asset"]["semantic_path"],
                              "spec": {"width": request.get("width", selected.get("width")), "height": request.get("height", selected.get("height")),
                                       "format": request.get("format", "mp4" if plan["kind"] == "video" else None), "codec": selected.get("codec"),
                                       "frame_rate": selected.get("frame_rate"), "duration_seconds": (request.get("layout") or {}).get("duration")},
                              "kind": plan["kind"], "stage": stage, "attempt": current.get("attempt", 0),
                              "received_bytes": current.get("received_bytes", 0),
                              "total_bytes": current.get("expected_bytes", current.get("total_bytes")), "verified": False})
        counts["total"] = len(self.plans)
        return {"kind": "snapshot", "lifecycle": self.lifecycle, "counts": counts, "items": items[:4],
                "active": len(items), "queued": counts["pending"] - sum(item["stage"] == "queued" for item in items),
                "needs_action": len(self.decisions), "stats": self.control.snapshot()}

    def emit(self, stage, plan=None, **values):
        self.control.emit({"kind": "stage", "stage": stage,
                           "item_id": plan["item_id"] if plan else None, **values})
        self.drain()

    def drain(self, active=None, queued=None):
        self.cancellation.poll()
        if active is not None:
            self.active_count = active
        if queued is not None:
            self.queued_count = queued
        active, queued = self.active_count, self.queued_count
        lookup = {plan["item_id"]: plan for plan in self.plans}
        events = self.control.drain_events()
        for event in events:
            plan = lookup.get(event.get("item_id"))
            if plan:
                event.update(semantic_path=plan["asset"]["semantic_path"], filename=plan["filename"],
                             ordinal=self.plans.index(plan) + 1, total=len(self.plans))
            event.update(active=active, queued=queued)
            if event.get("item_id") is not None:
                self.progress.setdefault(event["item_id"], {}).update(event)
            self.send_event(event)
        if self.v04:
            self.send_event(self.progress_snapshot(active, queued))
        flush = getattr(self.event_sink, "flush", None)
        if callable(flush):
            flush()

    def tools(self, *, parser=False):
        if parser:
            from .hls import require_parser
            require_parser()
        if self.media_tools is None:
            from .motion import MediaTools
            self.media_tools = MediaTools.discover(ffmpeg=self.environ.get("APPLE_ARTWORK_FFMPEG"),
                                                   ffprobe=self.environ.get("APPLE_ARTWORK_FFPROBE"),
                                                   control=self.control)
        self.manifest["media_tools"] = self.media_tools.to_dict()
        return self.media_tools

    def checkpoint(self):
        try:
            self.state.save()
        except ArtworkError:
            self.fatal = True
            self.control.cancel()
            raise

    def serial(self, operation):
        return _serial_call(self.control, self.drain, operation)

    def initial(self, manifest, *, created_at, legacy_record=None):
        self.manifest = manifest
        self.plans, self.issues = _make_plan(manifest)
        self.state.data = {"task_identity": self.identity, "task_fingerprint": fingerprint(self.identity),
                           "snapshot": {"created_at": created_at, "manifest": deepcopy(manifest),
                                        "files": deepcopy(self.plans)},
                           "files": {plan["item_id"]: {"status": "pending", "temporary": None,
                                                       "artifact": None, "validation_policy": None}
                                     for plan in self.plans},
                           "manifest": {"version": 1 if legacy_record else 0, "current": legacy_record,
                                        "previous": None, "temporary": None}, "manifest_pending": None}
        if self.v04:
            self.state.data.update(layout_version=self.layout_version, history=[], control=None)
            if getattr(self.args, "_parent_record", None) is not None:
                self.state.data["parent"] = deepcopy(self.args._parent_record)

    def validate_state(self):
        data = self.state.data
        try:
            if (not isinstance(data, dict) or data.get("task_fingerprint") != fingerprint(data["task_identity"])
                    or not isinstance(data.get("snapshot"), dict) or not isinstance(data.get("files"), dict)
                    or "manifest_pending" not in data):
                raise _invalid()
            if data["task_identity"] != self.identity:
                raise ArtworkError("TASK_CONFLICT", "This directory belongs to different task parameters; use a new output directory.", "download")
            snapshot = data["snapshot"]
            self.layout_version = data.get("layout_version", 1)
            if type(self.layout_version) is not int or self.layout_version not in {1, 2}:
                raise _invalid()
            if snapshot.get("created_at") is not None and not isinstance(snapshot["created_at"], str):
                raise _invalid()
            _validate_base(snapshot["manifest"], self.args, self.target)
            self.manifest = deepcopy(snapshot["manifest"])
            self.plans, self.issues = _make_plan(self.manifest, snapshot["files"])
            if snapshot["files"] != self.plans or set(data["files"]) != {plan["item_id"] for plan in self.plans}:
                raise _invalid()
            for plan in self.plans:
                record = data["files"][plan["item_id"]]
                if (not isinstance(record, dict) or record.get("status") not in
                        {"pending", "running", "prepared", "committed", "failed", "cancelled", "not_started", "conflict", "skipped"}
                        or not {"status", "temporary", "artifact", "validation_policy"} <= record.keys()
                        or (record["temporary"] is not None
                            and not valid_temporary_record(record["temporary"], plan["filename"]))):
                    raise _invalid()
                if record["status"] in {"prepared", "committed"}:
                    _check_artifact(plan, record["artifact"])
                    if not isinstance(record["validation_policy"], str):
                        raise _invalid()
                if record["status"] == "prepared" and (record["temporary"] is None or not record["temporary"]["sealed"]):
                    raise _invalid()
                if record["status"] == "running" and record["temporary"] is None:
                    raise _invalid()
                if record.get("error") is not None:
                    _error_value(record["error"])
            self.state.recover_manifest()
            if self.v04:
                data.setdefault("layout_version", self.layout_version)
                data.setdefault("history", [])
                data.setdefault("control", None)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise _invalid() from None

    def pending_inspection(self):
        """Finish only an interrupted first inspection of the frozen catalog."""
        from .hls import inspect_motion

        pending = [entry for entry in self.manifest["motion_inspections"]
                   if entry["status"] == "pending" or entry.get("planning_failed") is True]
        if not pending:
            return
        self.tools(parser=True)
        for entry in pending:
            self.control.check()
            self.emit("inspect", semantic_path=entry["semantic_path"])
            self.run["pending_inspection"]["attempted"] += 1
            asset = _asset_value(entry["references"][0])
            try:
                if self.v04:
                    error_record = entry.get("error") or {}
                    retry_at = error_record.get("retry_at") or error_record.get("context", {}).get("retry_at")
                    if retry_at is not None and retry_at > self.control.clock():
                        delay = retry_at - self.control.clock()
                        self.control.emit({"kind": "cooldown", "item_id": "issue_" + asset.asset_id,
                                           "retry_after_seconds": delay, "retry_at": retry_at})
                        deadline = self.control.deadline(60)
                        self.serial(lambda: self.control.wait(delay, deadline=deadline, category=error_record.get("category", "rendition")))
                previous = entry.get("inspection")
                kwargs = {"previous": previous} if previous is not None else {}
                inspection = self.serial(lambda: inspect_motion(asset, self.http, profile=self.args.motion_profile, **kwargs))
                entry.update(status=inspection.status, inspection=inspection.to_dict(), planning_failed=False,
                             inspection_captured_at=utc_now())
                entry.pop("error", None)
                if inspection.error is not None:
                    entry["error"] = inspection.error.to_dict()
                if inspection.supported:
                    self.run["pending_inspection"]["completed"] += 1
            except ArtworkError as error:
                partial = getattr(error, "inspection", None)
                entry.update(status="failed", error=error.to_dict(), planning_failed=not self.v04 or error.code == "CANCELLED",
                             inspection_captured_at=None,
                             inspection=partial.to_dict() if partial is not None else entry.get("inspection"))
                if self.v04:
                    self.state.data.setdefault("history", []).append({"action": "failure", "item_id": "issue_" + asset.asset_id,
                        "window_id": self.control.budget_snapshot()["window_id"], "code": error.code, "category": error.category,
                        "context": error.to_dict().get("context", {}), "at": utc_now()})
                if error.code == "CANCELLED":
                    self.cancellation.cancel()
            self.run["pending_inspection"]["checked_at"] = utc_now()
            self.plans, self.issues = _make_plan(self.manifest, self.plans)
            for issue in self.issues:
                if self.v04 and "issue_" + issue["asset_id"] in self.state.data.get("skipped_issues", []):
                    _skip_item(issue)
            for plan in self.plans:
                self.state.data["files"].setdefault(plan["item_id"], {"status": "pending", "temporary": None,
                                                                  "artifact": None, "validation_policy": None})
            self.state.data["snapshot"].update(manifest=deepcopy(self.manifest), files=deepcopy(self.plans))
            self.checkpoint()
            self.drain()
            if self.cancellation.user_cancelled:
                break

    def cleanup(self, record, plan):
        registered = record["temporary"]
        if registered is not None:
            directory = self.media(plan)
            if directory.temporary_exists(registered["name"]):
                directory.cleanup_temp(directory.restore_temp(registered))
            record["temporary"] = None
            self.checkpoint()

    def failure(self, plan, error, *, status="failed"):
        record = self.state.data["files"][plan["item_id"]]
        entry = _failed_download(_asset_value(plan["asset"]), error, references=plan["references"])
        entry.update(path=self.public_path(plan), status=status)
        if isinstance(error, OutputCommitError):
            entry["output"] = error.output_state()
        record.update(status=status, error=error.to_dict())
        retry_at = error.to_dict().get("retry_at") or error.to_dict().get("context", {}).get("retry_at")
        if retry_at is not None:
            record["retry_at"] = retry_at
        self.outcomes[plan["item_id"]] = entry
        if self.v04:
            failure_key = fingerprint({"error": error.to_dict(), "attempt": record.get("attempt_total", 0), "status": status})
            if record.get("last_failure_key") != failure_key:
                record["last_failure_key"] = failure_key
                self.state.data.setdefault("history", []).append({"action": "failure", "item_id": plan["item_id"],
                    "attempt": record.get("attempt_total", 0), "window_id": self.control.budget_snapshot()["window_id"],
                    "code": error.code, "category": error.category, "context": error.to_dict().get("context", {}), "at": utc_now()})
            self.emit("failed", plan, code=error.code, category=error.category,
                      **{key: value for key, value in error.to_dict().get("context", {}).items() if key in {"reason", "retry_at"}})
        return entry

    def decorated(self, plan, artifact, action):
        entry = deepcopy(artifact)
        entry.update(kind=plan["kind"], references=plan["references"], action=action)
        entry["path"] = self.public_path(plan)
        if plan["motion_declarations"]:
            entry["motion_declarations"] = plan["motion_declarations"]
        return entry

    def cleanup_committed(self, plan, record):
        try:
            self.cleanup(record, plan)
        except ArtworkError as error:
            self.errors.append(dict(error.to_dict(), asset_id=plan["asset"]["asset_id"],
                                    semantic_path=plan["asset"]["semantic_path"]))
            registered = record["temporary"]
            self.outcomes[plan["item_id"]]["output"] = {
                "path": plan["filename"], "committed": True,
                "residue": [registered["name"]] if registered is not None else []}
            if self.fatal:
                raise

    def publish(self, plan, temporary, artifact):
        _check_artifact(plan, artifact)
        record = self.state.data["files"][plan["item_id"]]
        record.update(status="prepared", temporary=temporary.record(), artifact=artifact,
                      validation_policy=VALIDATION_POLICIES[plan["kind"]])
        record.pop("error", None)
        self.checkpoint()
        self.emit("save", plan)
        try:
            self.media(plan).publish_temp(temporary)
        except OutputCommitError as error:
            if error.committed:
                entry = self.decorated(plan, artifact, "downloaded")
                entry["output"] = error.output_state()
                self.outcomes[plan["item_id"]] = entry
            raise
        record["status"] = "committed"
        self.outcomes[plan["item_id"]] = self.decorated(plan, artifact, "downloaded")
        self.checkpoint()
        self.emit("committed", plan)
        self.cleanup_committed(plan, record)

    def full_local_validation(self, plan, actual, filename=None):
        directory = self.media(plan)
        request = _request_value(plan)
        filename = plan["filename"] if filename is None else filename
        self.emit("validate", plan)
        if plan["kind"] == "image":
            body, identity = directory.read_file(filename, max_bytes=MAX_IMAGE_BYTES,
                                                   check=self.control.check)
            result = validate_image(body, request, declared_p3=_declared_p3(plan))
        else:
            from .motion import validate_motion
            tools = self.tools()
            with self.gate:
                result = validate_motion(directory._absolute / filename, request, tools, actual,
                                          control=self.control, item_id=plan["item_id"])
            identity = directory.file_identity(filename)
        if identity != actual["identity"] or directory.file_identity(filename) != actual["identity"]:
            raise ArtworkError("OUTPUT_CONFLICT", "Existing media changed during local validation; its bytes were preserved.", "download")
        return result

    def existing(self):
        for plan in self.plans:
            self.control.check()
            self.emit("check_existing", plan)
            record = self.state.data["files"][plan["item_id"]]
            directory = self.media(plan)
            try:
                if self.v04 and record["status"] == "skipped":
                    entry = _failed_download(_asset_value(plan["asset"]), _error_value(record.get("skip_reason") or record["error"]), references=plan["references"])
                    _skip_item(entry)
                    entry["path"] = self.public_path(plan)
                    self.outcomes[plan["item_id"]] = entry
                    continue
                if plan["blocked_error"] is not None:
                    self.failure(plan, _error_value(plan["blocked_error"]))
                    continue
                identity = directory.file_identity(plan["filename"])
                if record["status"] == "prepared" and identity is None:
                    if directory.temporary_exists(record["temporary"]["name"]):
                        temporary = directory.restore_temp(record["temporary"])
                        actual = directory.hash_file(temporary.name, max_bytes=MAX_IMAGE_BYTES if plan["kind"] == "image" else MAX_MOTION_BYTES,
                                                       check=self.control.check)
                        validation = _check_artifact(plan, record["artifact"])
                        if any(actual[key] != validation[key] for key in ("bytes", "sha256")):
                            raise ArtworkError("OUTPUT_CONFLICT", "Prepared media changed after validation; it was preserved.", "download")
                        if record["validation_policy"] != VALIDATION_POLICIES[plan["kind"]]:
                            record["local_verification"] = {
                                "checked_at": utc_now(), "method": "full_local_validation", "decoded_this_run": True,
                                "bytes": actual["bytes"], "sha256": actual["sha256"],
                                "validation_policy": VALIDATION_POLICIES[plan["kind"]],
                                "validation": self.full_local_validation(plan, actual, temporary.name)}
                        self.publish(plan, temporary, record["artifact"])
                        continue
                if identity is not None:
                    if record["status"] not in {"prepared", "committed", "conflict"} or record["artifact"] is None:
                        raise ArtworkError("OUTPUT_CONFLICT", "A media destination exists without a matching verified record; it was preserved.", "download")
                    validation = _check_artifact(plan, record["artifact"])
                    actual = directory.hash_file(plan["filename"], max_bytes=MAX_IMAGE_BYTES if plan["kind"] == "image" else MAX_MOTION_BYTES,
                                                   check=self.control.check)
                    if any(actual[key] != validation[key] for key in ("bytes", "sha256")):
                        raise ArtworkError("OUTPUT_CONFLICT", "Existing media differs from its verified length or SHA-256; it was preserved.", "download")
                    local = {"checked_at": utc_now(), "method": "length_sha256_and_frozen_request",
                             "bytes": actual["bytes"], "sha256": actual["sha256"],
                             "validation_policy": VALIDATION_POLICIES[plan["kind"]], "decoded_this_run": False}
                    if record["validation_policy"] != VALIDATION_POLICIES[plan["kind"]]:
                        local.update(method="full_local_validation", decoded_this_run=True,
                                     validation=self.full_local_validation(plan, actual))
                        record["validation_policy"] = VALIDATION_POLICIES[plan["kind"]]
                    record.update(status="committed", local_verification=local)
                    record.pop("error", None)
                    self.checkpoint()
                    entry = self.decorated(plan, record["artifact"], "reused")
                    entry["local_verification"] = local
                    self.outcomes[plan["item_id"]] = entry
                    self.emit("reused", plan)
                    self.cleanup_committed(plan, record)
                else:
                    self.cleanup(record, plan)
                    if self.v04 and record.get("error") is not None and record["status"] in {"failed", "conflict"}:
                        self.failure(plan, _error_value(record["error"]))
                        continue
                    record.update(status="pending", artifact=None, validation_policy=None)
                    record.pop("error", None)
            except ArtworkError as error:
                if error.code == "CANCELLED":
                    raise
                if self.fatal or error.code in {"RUN_STATE_INVALID", "RUN_STATE_WRITE_FAILED", "RUN_STATE_TOO_LARGE"}:
                    raise
                self.failure(plan, error, status="conflict")
        self.checkpoint()

    def worker(self, plan, temporary, attempt, deadline):
        http = self.http.fork(item_id=plan["item_id"], attempt=attempt)
        asset, request = _asset_value(plan["asset"]), _request_value(plan)
        self.control.emit({"kind": "stage", "stage": "download", "item_id": plan["item_id"],
                           "attempt": attempt, "received_bytes": 0})
        if plan["kind"] == "image":
            return prepare_image(asset, request, temporary, http, declared_p3=_declared_p3(plan),
                                 control=self.control, item_id=plan["item_id"], deadline=deadline)
        from .motion import prepare_motion
        return prepare_motion(asset, request, temporary, http, self.media_tools, control=self.control,
                              item_id=plan["item_id"], deadline=deadline, validation_gate=self.gate)

    def schedule(self):
        queue = [plan for plan in self.plans if plan["item_id"] not in self.outcomes]
        if any(plan["kind"] == "video" for plan in queue):
            self.tools()
        attempts, deadlines, active, submitted = {}, {}, {}, set()
        self.active_count, self.queued_count = 0, len(queue)
        for plan in queue:
            self.emit("queued", plan)
        with ThreadPoolExecutor(max_workers=self.args.jobs, thread_name_prefix="apple-artwork") as executor:
            while queue or active:
                try:
                    while queue and len(active) < self.args.jobs and not self.control.cancelled and not self.fatal:
                        ready = next((index for index, candidate in enumerate(queue)
                                      if self.state.data["files"][candidate["item_id"]].get("retry_at", 0) <= self.control.clock()), None)
                        if ready is None and active:
                            break
                        if ready is None:
                            ready = min(range(len(queue)), key=lambda index: self.state.data["files"][queue[index]["item_id"]].get("retry_at", 0))
                        plan = queue.pop(ready)
                        self.queued_count = len(queue)
                        item_id = plan["item_id"]
                        attempt = attempts[item_id] = attempts.get(item_id, 0) + 1
                        deadline = deadlines.setdefault(item_id, self.control.deadline(300))
                        record = self.state.data["files"][item_id]
                        if self.v04 and record.get("retry_at", 0) > self.control.clock():
                            try:
                                self.serial(lambda: self.control.wait(record["retry_at"] - self.control.clock(), deadline=deadline))
                            except ArtworkError as error:
                                self.failure(plan, error)
                                self.checkpoint()
                                continue
                        temporary = self.media(plan).allocate_temp(plan["filename"])
                        record.update(status="running", temporary=temporary.record(), artifact=None,
                                      validation_policy=None, attempt=attempt)
                        if self.v04:
                            record["attempt_total"] = record.get("attempt_total", 0) + 1
                            self.state.data.setdefault("history", []).append({"action": "attempt", "item_id": item_id,
                                  "attempt": record["attempt_total"], "window_id": self.control.budget_snapshot()["window_id"], "at": utc_now()})
                        self.checkpoint()
                        self.active_items.add(item_id)
                        active[executor.submit(self.worker, plan, temporary, attempt, deadline)] = (plan, temporary)
                        self.active_count = len(active)
                        submitted.add(item_id)
                    self.drain(len(active), len(queue))
                    if not active:
                        break
                    completed, _ = wait(active, timeout=0.05, return_when=FIRST_COMPLETED)
                    # When several finish together, commit in frozen-plan order.
                    for future in sorted(completed, key=lambda item: active[item][0]["ordinal"]):
                        plan, temporary = active.pop(future)
                        self.active_count = len(active)
                        item_id, artifact, error = plan["item_id"], None, None
                        try:
                            artifact = future.result()
                        except KeyboardInterrupt:
                            self.cancellation.cancel()
                            error = ArtworkError("CANCELLED", "The operation was cancelled.", "download")
                        except ArtworkError as failure:
                            error = failure
                        except Exception:
                            error = _internal()
                        if artifact is not None:
                            if not self.fatal:
                                self.publish(plan, temporary, artifact)
                            self.active_items.discard(item_id)
                            continue
                        record = self.state.data["files"][item_id]
                        if not self.fatal:
                            self.cleanup(record, plan)
                        if (error.retryable and attempts[item_id] < 3 and not self.control.cancelled and not self.fatal):
                            self.control.emit({"kind": "retry", "item_id": item_id, "attempt": attempts[item_id] + 1,
                                               "received_bytes": 0})
                            record["status"] = "pending"
                            queue.insert(0, plan)
                            self.queued_count = len(queue)
                        else:
                            self.failure(plan, error, status="cancelled" if error.code == "CANCELLED" else "failed")
                            if not self.fatal:
                                self.checkpoint()
                        self.active_items.discard(item_id)
                except KeyboardInterrupt:
                    self.cancellation.cancel()
                except ArtworkError as error:
                    self.errors.append(error.to_dict())
                    self.fatal = True
                    self.control.cancel()
                except Exception:
                    self.errors.append(_internal().to_dict())
                    self.fatal = True
                    self.control.cancel()
            self.active_items.clear()
            self.drain(0, 0)
        for plan in self.plans:
            if plan["item_id"] not in self.outcomes:
                error = ArtworkError("CANCELLED", "The file was not completed in this run.", "download")
                record = self.state.data["files"][plan["item_id"]]
                self.failure(plan, error, status="cancelled" if plan["item_id"] in submitted else "not_started")
        if not self.fatal:
            self.checkpoint()

    def decision_loop(self):
        """Only called after schedule has joined every worker and validator."""
        from .protocol import decision, validate_choice
        from .errors import recovery_for
        from .planning import derived_options
        while not self.fatal and not self.cancellation.user_cancelled:
            problems = [(plan["item_id"], self.outcomes[plan["item_id"]]["error"])
                        for plan in self.plans if self.outcomes.get(plan["item_id"], {}).get("status") in {"failed", "conflict"}]
            problems.extend(("issue_" + item["asset_id"], item["error"]) for item in self.issues if item.get("status") != "skipped")
            if not problems:
                self.decisions, self.lifecycle, self.control_exit = [], "completed", None
                return
            # A newly encountered failure produces a new content-bound decision
            # revision. No active work remains while a caller considers it.
            basis = {"task_id": self.run["task_fingerprint"], "snapshot": self.state.data["snapshot"],
                     "problems": problems,
                     "files": {key: {field: value.get(field) for field in ("status", "artifact", "error", "attempt_total")}
                               for key, value in self.state.data["files"].items()},
                     "conflicts": [value for value in self.prepared_plan["local_observations"]["files"] if value["status"] == "conflict"],
                     "history": [item for item in self.state.data.get("history", []) if item["action"] == "attempt"]}
            self.current_plan_id = fingerprint(basis)
            previous_basis = self.state.data.get("decision_basis")
            stored_contexts = (self.state.data.get("control") or {}).get("decisions", []) if previous_basis == self.current_plan_id else []
            proposed_contexts = self.prepared_plan["decisions"] if len(basis["history"]) == self.history_start else []
            contexts = []
            for item_id, error in problems:
                recovery = recovery_for(error)
                actions = ["defer", "skip"]
                if "retry" in recovery["allowed_actions"] or error["category"] == "validation":
                    actions.insert(0, "retry")
                actions.extend(derived_options(self.prepared_plan, [item_id]))
                context = decision(self.current_plan_id, "item_issue", error["code"], [item_id], actions)
                context["retry_at"] = recovery.get("retry_at")
                candidates = [item for item in [*stored_contexts, *proposed_contexts]
                              if item.get("item_ids") == [item_id] and item.get("reason") == error["code"]]
                chosen_context = next((item for item in candidates if item["decision_id"] in self.choices), None)
                if chosen_context is None and candidates:
                    chosen_context = candidates[0]
                if chosen_context is not None:
                    context = deepcopy(chosen_context)
                    context["retry_at"] = recovery.get("retry_at")
                display_items = [item for item in self.prepared_plan["items"] if item["item_id"] == item_id]
                context["display"] = {"items": [{key: item.get(key) for key in ("name", "name_key", "kind", "spec")} for item in display_items]}
                contexts.append(context)
            self.state.data["decision_basis"] = fingerprint(basis)
            offered = {context["decision_id"] for context in contexts}
            if set(self.choices) - offered:
                raise ArtworkError("DECISION_STALE", "A supplied choice does not match any current decision.", "input")
            self.decisions, self.lifecycle = contexts, "needs_action"
            if contexts and len({context["plan_id"] for context in contexts}) == 1:
                self.current_plan_id = contexts[0]["plan_id"]
            self.emit("needs_action")
            retry_ids, deferred, unanswered = set(), False, False
            for context in contexts:
                choice = self.choices.pop(context["decision_id"], None)
                if choice is None and self.decision_provider is not None:
                    choice = self.decision_provider(deepcopy(context))
                if choice is None:
                    unanswered = True
                    break
                chosen = validate_choice(context, choice)
                self.state.data.setdefault("history", []).append({**chosen, "at": utc_now()})
                if chosen["action"] == "defer":
                    deferred = True
                    break
                if chosen["action"] == "derive":
                    self.next_task = {"action": "derive", "params": deepcopy(chosen["params"]),
                                      "decision_id": chosen["decision_id"], "parent": {
                                          "task_id": self.run["task_fingerprint"],
                                          "version": self.prepared_plan["destination"].get("version"),
                                          "decision_id": chosen["decision_id"],
                                          "changes": deepcopy(chosen["params"])}}
                    deferred = True
                    break
                item_id = context["item_ids"][0]
                if chosen["action"] == "retry":
                    retry_ids.add(item_id)
                elif chosen["action"] == "skip":
                    if item_id.startswith("issue_"):
                        for item in self.issues:
                            if "issue_" + item["asset_id"] == item_id:
                                _skip_item(item)
                        self.state.data.setdefault("skipped_issues", []).append(item_id)
                    else:
                        _skip_item(self.state.data["files"][item_id])
                        _skip_item(self.outcomes[item_id])
                        self.emit("skipped", next(plan for plan in self.plans if plan["item_id"] == item_id))
            if deferred or unanswered:
                self.lifecycle = "deferred" if deferred else "needs_action"
                self.control_exit = 9 if deferred else 8
                self.decisions = [context for context in contexts if not all(
                    self.state.data["files"].get(item_id, {}).get("status") == "skipped"
                    or item_id in self.state.data.get("skipped_issues", []) for item_id in context["item_ids"])]
                return
            if not retry_ids:
                continue
            self.lifecycle, self.decisions = "running", []
            self.control.begin_attempt_window()
            for item_id in retry_ids:
                if item_id.startswith("issue_"):
                    asset_id = item_id.removeprefix("issue_")
                    for entry in self.manifest["motion_inspections"]:
                        if any(value["asset_id"] == asset_id for value in entry["references"]):
                            entry["planning_failed"] = True
                else:
                    self.outcomes.pop(item_id, None)
                    record = self.state.data["files"][item_id]
                    record["status"] = "pending"
                    record.pop("error", None)
            self.checkpoint()
            self.pending_inspection()
            self.schedule()

    def control_result(self):
        destination = self.prepared_plan["destination"]
        version = destination.get("version")
        reference = self.run["task_fingerprint"] + "@" + str(version) if version else None
        result = {"protocol_version": 1, "lifecycle": self.lifecycle, "task_id": self.run["task_fingerprint"],
                "plan_id": self.current_plan_id, "decision_required": bool(self.decisions) and self.lifecycle in {"needs_action", "deferred"}, "decisions": self.decisions,
                "resume": {"command": "resume", "task_ref": reference, "task_id": self.run["task_fingerprint"],
                           "requires_task_dir": destination["mode"] == "exact"},
                "history": deepcopy(self.state.data.get("history", [])) if self.state.data else []}
        if self.next_task is not None:
            result["next_task"] = deepcopy(self.next_task)
        if self.state.data and self.state.data.get("parent"):
            result["parent"] = deepcopy(self.state.data["parent"])
        return result

    def finish(self, *, save=True):
        self.run["cancelled"] = self.cancellation.user_cancelled
        entries = [self.outcomes[plan["item_id"]] for plan in self.plans
                   if plan["item_id"] in self.outcomes and self.outcomes[plan["item_id"]]["status"] not in {"cancelled", "not_started"}]
        order = {item["asset_id"]: index for index, item in enumerate((self.manifest.get("selection") or {}).get("selected", []))}
        self.manifest["downloads"] = sorted([*entries, *self.issues], key=lambda item: order.get(item["asset_id"], -1))
        self.manifest["errors"] = [dict(item["error"], asset_id=item["asset_id"], semantic_path=item["semantic_path"])
                                   for item in self.manifest["downloads"] if "error" in item]
        self.manifest["errors"].extend(self.errors)
        if self.run["cancelled"] and not any(item["code"] == "CANCELLED" for item in self.manifest["errors"]):
            self.manifest["errors"].append(ArtworkError("CANCELLED", "The command was cancelled.", "download").to_dict())
        counts = {"total": len(self.plans), "new_downloaded": 0, "reused": 0, "failed": 0,
                  "cancelled": 0, "not_started": 0,
                  "semantic_references": len((self.manifest.get("selection") or {}).get("selected", []))}
        if self.v04 and any(item.get("status") == "skipped" for item in self.outcomes.values()):
            counts["skipped"] = 0
        skipped_issues = sum(item.get("status") == "skipped" for item in self.issues)
        if self.v04 and skipped_issues:
            counts["skipped_issues"] = skipped_issues
        for plan in self.plans:
            item = self.outcomes.get(plan["item_id"])
            if item is None:
                counts["not_started"] += 1
            elif item["status"] == "downloaded":
                counts["reused" if item["action"] == "reused" else "new_downloaded"] += 1
            elif item["status"] in {"cancelled", "not_started"}:
                counts[item["status"]] += 1
            elif self.v04 and item["status"] == "skipped":
                counts["skipped"] += 1
            else:
                counts["failed"] += 1
        self.run.update(counts=counts, stats={**self.control.snapshot(), "local_reused": counts["reused"]})
        self.run["interrupted"] = [
            {"item_id": plan["item_id"], "asset_id": plan["asset"]["asset_id"],
             "semantic_path": plan["asset"]["semantic_path"], "path": plan["filename"],
             "status": self.outcomes.get(plan["item_id"], {}).get("status", "not_started")}
            for plan in self.plans if plan["item_id"] not in self.outcomes
            or self.outcomes[plan["item_id"]]["status"] in {"cancelled", "not_started"}]
        self.manifest["run"] = self.run
        code = _status(self.manifest)
        _coverage(self.manifest)
        self.manifest["status_scope"] = "download_results"
        if self.v04:
            if self.cancellation.user_cancelled:
                self.lifecycle, self.decisions, self.control_exit = "cancelled", [], None
            elif self.fatal:
                self.lifecycle, self.control_exit = "failed", None
            self.manifest["control"] = self.control_result()
            if self.journal is not None:
                self.journal.flush()
                self.journal.summary(result=self.manifest, unresolved=self.decisions)
                self.journal_warnings.extend(self.journal.warnings)
            if self.journal_warnings:
                self.manifest.setdefault("warnings", []).extend(self.journal_warnings)
        if self.state.data is not None:
            self.manifest["run"]["residue"] = sorted(set(residue_paths(self.output) + self.state.unknown_residue()))
        if save and not self.fatal and self.state.data is not None:
            try:
                if self.v04:
                    self.state.data["control"] = deepcopy(self.manifest["control"])
                    self.checkpoint()
                self.manifest["manifest_report"] = self.state.save_manifest(self.manifest)
            except ArtworkError as error:
                self.manifest["download_status"] = self.manifest["status"]
                self.manifest["status_scope"] = "runtime"
                self.manifest["errors"].append(error.to_dict())
                self.manifest["manifest_report"] = {"path": "manifest.json", "status": "failed"}
                if isinstance(error, OutputCommitError):
                    self.manifest["manifest_report"].update(error.output_state())
                self.run["residue"] = sorted(set(residue_paths(self.output) + self.state.unknown_residue()))
                code = _status(self.manifest)
                _coverage(self.manifest)
                if self.v04:
                    self.lifecycle, self.control_exit = "failed", None
                    self.manifest["control"] = self.control_result()
                    code = EXIT_CODES.get(error.category, 1)
        else:
            self.manifest["manifest_report"] = {"path": "manifest.json", "status": "not_saved"}
        self.drain()
        if self.journal is not None:
            self.journal.summary(result=self.manifest, unresolved=self.decisions)
            self.journal.close()
            self.manifest.setdefault("warnings", []).extend(warning for warning in self.journal.warnings if warning not in self.manifest.get("warnings", []))
        if self.v04 and self.control_exit is not None and self.manifest["manifest_report"]["status"] == "saved":
            code = self.control_exit
        return self.manifest, 130 if self.cancellation.user_cancelled else code

    def import_legacy(self, body):
        try:
            old = loads_json(body)
            if self.args.response or old.get("provider") not in {"web", "catalog"} or "run" in old:
                raise _invalid()
            _validate_base(old, self.args, self.target)
            manifest = deepcopy(old)
            for name in ("manifest_report", "status_scope", "download_status", "run"):
                manifest.pop(name, None)
            manifest.update(downloads=[], errors=[], status="success")
            self.initial(manifest, created_at=None, legacy_record={"bytes": len(body), "sha256": sha256(body).hexdigest()})
            by_path = {}
            for entry in old["downloads"]:
                if entry.get("status") != "downloaded":
                    continue
                if entry.get("path") in by_path:
                    raise _invalid()
                by_path[entry["path"]] = entry
            for plan in self.plans:
                entry = by_path.pop(plan["filename"], None)
                if entry is None:
                    continue
                _check_artifact(plan, entry)
                if entry.get("references") != plan["references"]:
                    raise _invalid()
                self.state.data["files"][plan["item_id"]].update(status="committed", artifact=deepcopy(entry),
                                                               validation_policy="legacy-unverified")
            if by_path:
                raise _invalid()
            self.run["legacy_import"] = True
            self.checkpoint()
        except ArtworkError as error:
            if error.code in {"RUN_STATE_WRITE_FAILED", "RUN_STATE_TOO_LARGE"}:
                raise
            raise ArtworkError("LEGACY_MANIFEST_UNRECOGNIZED", "The old manifest cannot reconstruct this online task safely; use a new output directory.", "download") from None
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ArtworkError("LEGACY_MANIFEST_UNRECOGNIZED", "The old manifest lacks required task identity or validation evidence; use a new output directory.", "download") from None

    def run_task(self, client, payload):
        try:
            if self.state.data is not None:
                self.resumed = True
                self.validate_state()
            else:
                try:
                    legacy, _ = self.output.read_file("manifest.json", max_bytes=MAX_JSON_BYTES)
                except FileNotFoundError:
                    legacy = None
                if legacy is not None:
                    self.resumed = True
                    self.import_legacy(legacy)
                else:
                    self.emit("metadata")
                    manifest = deepcopy(self.prepared_plan["snapshot"]["manifest"]) if self.v04 else _new_manifest(self.args, self.target, client, payload, self.serial)
                    self.manifest = manifest
                    if self.v04:
                        self.initial(manifest, created_at=self.prepared_plan["snapshot"]["created_at"])
                        self.checkpoint()
                    else:
                        self.initialize_legacy_selection(manifest)
            if self.v04:
                from .diagnostics import EventJournal
                try:
                    journal_directory = self.resources.enter_context(self.state.metadata.child("diagnostics", create=True))
                    self.journal = EventJournal(journal_directory, task_id=self.run["task_fingerprint"])
                except ArtworkError:
                    self.journal_warnings.append({"code": "DIAGNOSTICS_UNAVAILABLE", "message": "Diagnostic logging is unavailable; task-state persistence is independent."})
                self.history_start = sum(item["action"] == "attempt" for item in self.state.data.get("history", []))
                for item in self.issues:
                    if "issue_" + item["asset_id"] in self.state.data.get("skipped_issues", []):
                        _skip_item(item)
            self.run.update(resumed=self.resumed,
                            snapshot={"created_at": self.state.data["snapshot"]["created_at"],
                                      "remote_freshness": "not_rechecked" if self.resumed else
                                      "parent_snapshot" if getattr(self.args, "_source_manifest", None) is not None else
                                      "offline_response" if self.args.response else "fetched_this_run"})
            self.pending_inspection()
            self.existing()
            self.schedule()
            if self.v04:
                self.decision_loop()
        except KeyboardInterrupt:
            self.cancellation.cancel()
        except ArtworkError as error:
            if error.code == "CANCELLED":
                self.cancellation.cancel()
            else:
                self.errors.append(error.to_dict())
            if error.code in {"RUN_STATE_INVALID", "TASK_CONFLICT", "MANIFEST_CONFLICT", "LEGACY_MANIFEST_UNRECOGNIZED",
                              "RUN_STATE_WRITE_FAILED", "RUN_STATE_TOO_LARGE", "DECISION_STALE", "INVALID_DECISION"}:
                self.fatal = True
            if error.code in {"RUN_STATE_INVALID", "TASK_CONFLICT", "MANIFEST_CONFLICT", "LEGACY_MANIFEST_UNRECOGNIZED"}:
                self.manifest = {"schema_version": 2, "mode": "download", "target": self.target.to_dict(),
                                 "downloads": [], "errors": [], "status": "failed"}
                self.plans, self.issues, self.outcomes = [], [], {}
        except Exception:
            self.errors.append(_internal().to_dict())
            self.fatal = True
        try:
            return self.finish(save=self.state.data is not None)
        finally:
            self.resources.close()

    def initialize_legacy_selection(self, manifest):
        """The original execution path keeps its established dependency timing."""
        assets = tuple(_asset_value(value) for value in manifest["assets"])
        evidence = manifest["source_evidence"]
        source = SourceEvidence(evidence["requested_storefront"], evidence["requested_locale"],
                 tuple(StorefrontObservation(**{**item, "storefront": item["storefront"] or ""})
                       for item in evidence["observations"]))
        manifest["selection"] = select(DiscoveryResult(self.target, assets, source_evidence=source),
                                       _selector(self.args)).to_dict()
        motion = [item for item in manifest["selection"]["selected"] if item["kind"] == "video"]
        if motion:
            self.tools(parser=True)
        groups = {}
        for item in motion:
            groups.setdefault(item["url"], []).append(item)
        manifest["motion_inspections"] = [
            {"asset_id": values[0]["asset_id"], "semantic_path": values[0]["semantic_path"],
             "references": values, "status": "pending", "planning_failed": True,
             "inspection": None, "inspection_captured_at": None}
            for values in groups.values()]
        self.initial(manifest, created_at=utc_now())
        self.checkpoint()


def _list_target(args, target, client, payload, http, call):
    manifest = _new_manifest(args, target, client, payload, call)
    if args.inspect_motion:
        _inspect_motion_assets((_asset_value(value) for value in manifest["assets"] if value["role"] == "motion"),
                               manifest, http, args.motion_profile, call)
    _coverage(manifest)
    return manifest, _status(manifest)


def _list(args, target, client, payload, http, call):
    if not args.compare_storefronts:
        return _list_target(args, target, client, payload, http, call)
    storefronts = list(dict.fromkeys([target.key.storefront, *args.compare_storefronts]))
    results, parsed = [], urlsplit(target.input_url)
    for storefront in storefronts:
        parts = parsed.path.split("/")
        parts[1] = storefront
        compared = parse_url(urlunsplit((parsed.scheme, parsed.netloc, "/".join(parts), parsed.query, "")))
        try:
            result, _ = _list_target(args, compared, client, None, http, call)
        except ArtworkError as error:
            result = {**_error_result(error), "target": compared.to_dict(), "mode": "list", "provider": args.provider}
        results.append(result)
        if http.control.cancelled:
            break
    manifest = dict(results[0])
    manifest["errors"] = [dict(error, storefront=result["target"]["key"]["storefront"])
                          for result in results for error in result["errors"]]
    baseline = {_comparison_signature(asset): asset for asset in results[0].get("assets", [])}
    differences = []
    for result in results[1:]:
        other = {_comparison_signature(asset): asset for asset in result.get("assets", [])}
        ready = "assets" in results[0] and "assets" in result
        input_fields, compared_fields = results[0].get("coverage", {}).get("fields", {}), result.get("coverage", {}).get("fields", {})
        fields = [{"field": field, "input_state": input_fields.get(field, {}).get("state", "unchecked"),
                   "compared_state": compared_fields.get(field, {}).get("state", "unchecked")}
                  for field in sorted(input_fields.keys() | compared_fields.keys())
                  if input_fields.get(field, {}).get("state") != compared_fields.get(field, {}).get("state")]
        differences.append({"storefront": result["target"]["key"]["storefront"], "status": "compared" if ready else "unavailable",
                            "field_states": fields if ready else [],
                            "only_in_input": [baseline[key] for key in sorted(baseline.keys() - other.keys())] if ready else [],
                            "only_in_compared": [other[key] for key in sorted(other.keys() - baseline.keys())] if ready else []})
    manifest["comparison"] = {"locale": target.locale, "requested_storefronts": storefronts,
                              "results": results, "differences": differences}
    return manifest, _status(manifest)


def execute(args, *, http, environ, media_tools=None, event_sink=None, plan=None, decision_provider=None, choices=(),
            created_directory_identity=None):
    """The only application entry point; CLI owns presentation, not execution."""
    target = parse_url(args.url)
    _check_options(args, target)
    jobs = getattr(args, "jobs", None)
    args.jobs = 2 if jobs is None else jobs
    if type(args.jobs) is not int or not 1 <= args.jobs <= 4 or (args.list and getattr(args, "jobs_explicit", False)):
        raise ArtworkError("INVALID_ARGUMENTS", "--jobs accepts 1 to 4 in download mode only.", "input")
    payload = load_json(args.response) if args.response and not getattr(args, "_resume_identity", None) else None
    identity = deepcopy(getattr(args, "_resume_identity", None)) or _task_identity(args, target, payload)
    if plan is not None:
        from .planning import validate_plan
        validate_plan(plan)
        if plan["identity"] != identity:
            raise ArtworkError("PLAN_STALE", "Task parameters or offline content changed; inspect a new plan.", "input")
        _validate_base(plan["snapshot"]["manifest"], args, target)
    client = AppleMusicClient(http, provider=args.provider,
                              developer_token=environ.get(args.token_env) if args.provider == "catalog" else None)
    with _Cancellation(http.control) as cancellation:
        if args.list:
            try:
                def drain():
                    cancellation.poll()
                    for event in http.control.drain_events():
                        if event_sink is not None:
                            event_sink(event)
                    flush = getattr(event_sink, "flush", None)
                    if callable(flush):
                        flush()
                if event_sink is not None:
                    event_sink({"kind": "stage", "stage": "metadata", "item_id": None})
                manifest, code = _list(args, target, client, payload, http,
                                       lambda operation: _serial_call(http.control, drain, operation))
                manifest["run"] = {"run_id": getattr(event_sink, "run_id", None) or str(uuid4()), "cancelled": False,
                                   "stats": http.control.snapshot()}
                if cancellation.user_cancelled or http.control.cancelled:
                    manifest["run"]["cancelled"] = True
                    if not any(error["code"] == "CANCELLED" for error in manifest["errors"]):
                        manifest["errors"].append(ArtworkError("CANCELLED", "The command was cancelled.", "download").to_dict())
                    _status(manifest)
                    return manifest, 130
                return manifest, code
            except KeyboardInterrupt:
                cancellation.cancel()
                raise ArtworkError("CANCELLED", "The command was cancelled.", "download") from None
        destination = plan["destination"]["path"] if plan is not None else args.output or Path("output") / target.key.resource_id
        binding = getattr(args, "_selection_binding", None)
        existing_plan = (plan is not None and plan["local_observations"]["exists"]
                         and plan["local_observations"]["state_sha256"] is not None)
        if binding is not None:
            from .task_queries import open_bound_output, stale
            if os.path.abspath(destination) != binding.path:
                raise stale()
        if existing_plan:
            from .planning import admit_existing_plan, open_existing_plan_output
        def admit(output, metadata=None, lock_fd=None):
            if binding is not None:
                binding.admit(output, metadata, lock_fd)
            if existing_plan:
                admit_existing_plan(plan, output, metadata, lock_fd)
        output_context = (open_bound_output(binding) if binding is not None else
                          open_existing_plan_output(plan) if existing_plan else OutputDirectory(destination))
        with output_context as output:
            try:
                if plan is not None and not plan["local_observations"]["exists"] and not output.created:
                    from .planning import revalidate
                    revalidate(plan, output, RunState(output), created_directory_identity=created_directory_identity)
                with RunState(output, admission=admit if binding is not None or existing_plan else None,
                              require_existing_lock=binding is not None,
                              require_existing_metadata=existing_plan) as state:
                    if plan is not None:
                        from .planning import revalidate
                        revalidate(plan, output, state, created_directory_identity=created_directory_identity)
                    coordinator = _Coordinator(args, target, identity, output, state, http, environ,
                                               media_tools, event_sink, cancellation, plan=plan,
                                               decision_provider=decision_provider, choices=choices)
                    return coordinator.run_task(client, payload)
            except ArtworkError as error:
                manifest = {**_error_result(error), "target": target.to_dict(), "mode": "download",
                            "manifest_report": {"path": "manifest.json", "status": "not_saved"},
                            "run": {"run_id": str(uuid4()), "task_fingerprint": fingerprint(identity),
                                    "resumed": False, "cancelled": cancellation.user_cancelled,
                                    "jobs": args.jobs, "stats": http.control.snapshot(), "residue": residue_paths(output)}}
                if plan is not None:
                    manifest["control"] = {"protocol_version": 1, "lifecycle": "planned" if error.code == "PLAN_STALE" else "failed",
                                           "task_id": fingerprint(identity), "plan_id": plan["plan_id"],
                                           "decision_required": False, "decisions": [], "resume": None}
                    if hasattr(error, "updated_plan"):
                        manifest["control"].update(plan_id=error.updated_plan["plan_id"], updated_plan=error.updated_plan)
                return manifest, 130 if cancellation.user_cancelled else EXIT_CODES.get(error.category, 1)

