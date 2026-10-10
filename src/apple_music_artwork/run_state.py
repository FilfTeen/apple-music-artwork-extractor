"""Locked, bounded task snapshots and recoverable metadata publication."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import os
import re
import stat

from .download import OutputDirectory, OutputCommitError
from .errors import ArtworkError
from .jsonio import MAX_JSON_BYTES, dumps_json, loads_json


STATE_DIRECTORY = ".apple-artwork-state"
STATE_VERSION = 2
_DIGEST = re.compile(r"[0-9a-f]{64}")
_TEMPORARY = re.compile(r"\.apple-artwork-[0-9a-f]{32}\.part")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_bytes(value):
    body = dumps_json(value).encode("utf-8")
    if len(body) > MAX_JSON_BYTES:
        raise ArtworkError("RUN_STATE_TOO_LARGE", "Task state exceeds the supported JSON size limit.", "download")
    # Output serialization is not itself a depth check.
    loads_json(body)
    return body


def fingerprint(value):
    return sha256(json_bytes(value)).hexdigest()


def _invalid(message="Task state is damaged or cannot be recognized; use a new output directory."):
    return ArtworkError("RUN_STATE_INVALID", message, "download")


def _is_digest(value):
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def valid_temporary_record(value, final_name=None):
    return (isinstance(value, dict) and isinstance(value.get("name"), str)
            and _TEMPORARY.fullmatch(value["name"]) is not None
            and isinstance(value.get("final_name"), str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", value["final_name"]) is not None
            and (final_name is None or value["final_name"] == final_name)
            and all(type(value.get(key)) is int and value[key] >= 0
                    for key in ("device", "inode", "size", "mtime_ns"))
            and type(value.get("sealed")) is bool)


def valid_hash_record(value, *, optional=False):
    if value is None:
        return optional
    return (isinstance(value, dict) and type(value.get("bytes")) is int and value["bytes"] > 0
            and value["bytes"] <= MAX_JSON_BYTES and _is_digest(value.get("sha256")))


def residue_paths(output):
    """Inventory only; an unregistered name never grants cleanup authority."""
    residue = [name for name in output.names() if _TEMPORARY.fullmatch(name)]
    for name in ("images", "videos"):
        try:
            with output.child(name) as media:
                residue.extend(name + "/" + leaf for leaf in media.names() if _TEMPORARY.fullmatch(leaf))
        except FileNotFoundError:
            pass
        except ArtworkError:
            residue.append(name)
    try:
        fd = os.open(STATE_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=output._fd)
    except FileNotFoundError:
        return residue
    except OSError:
        return [*residue, STATE_DIRECTORY]
    try:
        residue.extend(STATE_DIRECTORY + "/" + name for name in os.listdir(fd) if _TEMPORARY.fullmatch(name))
    finally:
        os.close(fd)
    return sorted(residue)


def _document(body):
    if len(body) > MAX_JSON_BYTES:
        raise _invalid()
    try:
        return loads_json(body)
    except ArtworkError:
        raise _invalid() from None


class RunState:
    """One writer holds flock until workers and child processes have stopped.

    State uses a registered candidate and a previous revision. A transaction
    marker authorizes only those exact old/new hashes, never an arbitrary file
    found beside them. Files not named by a durable record are retained.
    """

    def __init__(self, output: OutputDirectory, *, admission=None, require_existing_lock=False,
                 require_existing_metadata=False):
        self.output = output
        self.admission = admission
        self.require_existing_lock = require_existing_lock
        self.require_existing_metadata = require_existing_metadata or require_existing_lock
        self.metadata = None
        self.lock_fd = None
        self.data = None
        self.revision = 0
        self.current_sha256 = None
        self.previous_sha256 = None
        self.loaded_version = STATE_VERSION
        self.upgrade_from = None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:
            raise ArtworkError("OUTPUT_PLATFORM_UNSUPPORTED", "Task recovery requires POSIX file locking.", "download") from None
        admitted = False
        try:
            if self.admission is not None:
                self.admission(self.output)
            self.metadata = self.output.child(STATE_DIRECTORY, create=not self.require_existing_metadata).__enter__()
            self.lock_fd = os.open("lock", os.O_RDWR | os.O_NOFOLLOW | (os.O_NONBLOCK if self.require_existing_lock else os.O_CREAT),
                                   0o600, dir_fd=self.metadata._fd)
            if not stat.S_ISREG(os.fstat(self.lock_fd).st_mode):
                if self.require_existing_lock:
                    raise ArtworkError("PLAN_STALE", "The selected task lock changed; inspect the current task list again.", "input")
                raise _invalid("Task lock is not an ordinary file.")
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ArtworkError("OUTPUT_BUSY", "Another process is using this output directory; wait for it to finish.", "download") from None
            if self.admission is not None:
                self.admission(self.output, self.metadata, self.lock_fd)
            admitted = True
            self._recover_state_transition()
            self._load()
            return self
        except (ArtworkError, OSError) as error:
            self.__exit__(None, None, None)
            if self.admission is not None and not admitted and getattr(error, "code", None) != "OUTPUT_BUSY":
                raise ArtworkError("PLAN_STALE", "The selected task directory or existing lock changed; inspect the current task list again.", "input") from None
            raise
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.metadata is not None:
            self.metadata.__exit__(*args)
            self.metadata = None

    def _read(self, name):
        try:
            return self.metadata.read_file(name, max_bytes=MAX_JSON_BYTES)[0]
        except FileNotFoundError:
            return None
        except ArtworkError:
            raise _invalid() from None

    def _envelope(self, body):
        value = _document(body)
        if (not isinstance(value, dict) or type(value.get("state_version")) is not int
                or value.get("state_version") not in {1, STATE_VERSION}
                or type(value.get("revision")) is not int or value["revision"] < 1
                or not isinstance(value.get("payload"), dict)
                or not _is_digest(value.get("payload_sha256"))
                or value["payload_sha256"] != fingerprint(value["payload"])
                or (value.get("previous_sha256") is not None and not _is_digest(value["previous_sha256"]))):
            raise _invalid()
        return value

    @classmethod
    def peek(cls, output):
        """Read committed state without creating a lock or repairing anything."""
        state = cls(output)
        try:
            metadata = output.child(STATE_DIRECTORY, create=False).__enter__()
        except FileNotFoundError:
            return None
        state.metadata = metadata
        try:
            if state._read("state-transition.json") is not None:
                raise ArtworkError("RUN_STATE_RECOVERY_REQUIRED", "Task state has a pending transaction; resume it to recover safely.", "download")
            value = state._load()
            if state.data is None:
                return None
            return {**value, "sha256": state.current_sha256}
        finally:
            metadata.__exit__(None, None, None)

    @classmethod
    def peek_recovery(cls, output):
        """Inspect a registered candidate without publishing it, for execution admission."""
        state = cls(output)
        try:
            metadata = output.child(STATE_DIRECTORY).__enter__()
        except FileNotFoundError:
            return None
        state.metadata = metadata
        try:
            body = state._read("state-transition.json")
            if body is None:
                return cls.peek(output)
            marker = _document(body)
            if (not isinstance(marker, dict) or type(marker.get("transition_version")) is not int
                    or marker.get("transition_version") != 1 or not _is_digest(marker.get("to_sha256"))
                    or any(marker.get(key) is not None and not _is_digest(marker[key]) for key in ("from_sha256", "from_previous_sha256"))
                    or not valid_temporary_record(marker.get("temporary"), "state.json") or not marker["temporary"]["sealed"]):
                raise _invalid()
            current = state._read("state.json")
            current_hash = sha256(current).hexdigest() if current is not None else None
            if current_hash == marker["to_sha256"]:
                candidate = current
            elif current_hash == marker.get("from_sha256"):
                temporary = metadata.restore_temp(marker["temporary"])
                candidate = state._read(temporary.name)
            else:
                raise _invalid()
            if candidate is None or sha256(candidate).hexdigest() != marker["to_sha256"]:
                raise _invalid()
            value = state._envelope(candidate)
            if value.get("previous_sha256") != marker.get("from_sha256"):
                raise _invalid()
            if current is not None and current_hash != marker["to_sha256"]:
                if value["revision"] != state._envelope(current)["revision"] + 1:
                    raise _invalid()
            elif current is None and value["revision"] != 1:
                raise _invalid()
            previous = state._read("state-previous.json")
            previous_hash = sha256(previous).hexdigest() if previous is not None else None
            if current_hash == marker["to_sha256"] or current is None:
                # Once published, recovery cannot rebuild the prior revision.
                # A first revision must also have no unregistered predecessor.
                if previous_hash != marker.get("from_sha256"):
                    raise _invalid("The registered previous task-state revision is missing or changed.")
                if previous is not None:
                    state._envelope(previous)
            elif previous_hash not in {marker.get("from_previous_sha256"), marker.get("from_sha256")}:
                # Before publication, the backup may still be old or may have
                # already advanced. _write_internal accepts exactly these two
                # states; it must not adopt an unrelated existing backup.
                raise _invalid("The previous task-state proof cannot complete the registered transition.")
            proof = value.get("upgrade_from")
            if proof is not None:
                original = state._read("state-v1-original.json")
                if (not isinstance(proof, dict) or set(proof) != {"sha256", "revision"} or original is None
                        or type(proof["revision"]) is not int or sha256(original).hexdigest() != proof["sha256"]
                        or state._envelope(original)["state_version"] != 1 or state._envelope(original)["revision"] != proof["revision"]):
                    raise _invalid()
            return {**value, "sha256": marker["to_sha256"], "recovery_required": True}
        finally:
            metadata.__exit__(None, None, None)

    def _load(self):
        body = self._read("state.json")
        if body is None:
            if any(name not in {"lock", "allocation.json"} for name in self.metadata.names()):
                raise _invalid("Unrecognized recovery files have been retained; use a new output directory.")
            return
        envelope = self._envelope(body)
        self.data = envelope["payload"]
        self.revision = envelope["revision"]
        self.current_sha256 = sha256(body).hexdigest()
        self.previous_sha256 = envelope.get("previous_sha256")
        self.loaded_version = envelope["state_version"]
        self.upgrade_from = envelope.get("upgrade_from")
        if self.loaded_version == 2 and self.upgrade_from is None and self._read("state-v1-original.json") is not None:
            raise _invalid("The independent pre-upgrade state proof is not registered.")
        if self.upgrade_from is not None:
            original = self._read("state-v1-original.json")
            if (not isinstance(self.upgrade_from, dict) or original is None
                    or set(self.upgrade_from) != {"sha256", "revision"}
                    or type(self.upgrade_from.get("revision")) is not int
                    or sha256(original).hexdigest() != self.upgrade_from.get("sha256")
                    or self._envelope(original)["state_version"] != 1
                    or self._envelope(original)["revision"] != self.upgrade_from["revision"]):
                raise _invalid("The independent pre-upgrade state proof is missing or changed.")
        previous = self._read("state-previous.json")
        if self.previous_sha256 is not None:
            if previous is None or sha256(previous).hexdigest() != self.previous_sha256:
                raise _invalid("The registered previous task-state revision is missing or changed.")
            self._envelope(previous)
        elif previous is not None:
            raise _invalid()
        return envelope

    def _remove_registered(self, temporary):
        if self.metadata.temporary_exists(temporary.name):
            self.metadata.cleanup_temp(temporary)

    def _write_internal(self, name, body, *, expected_sha256=None):
        """Only this coordinator updates recognized metadata under its lock."""
        existing = self._read(name)
        actual = sha256(existing).hexdigest() if existing is not None else None
        if actual == sha256(body).hexdigest():
            return
        if actual != expected_sha256:
            raise _invalid("A registered metadata file changed outside this task.")
        temporary = self.metadata.allocate_temp(name)
        try:
            temporary.write_stream(lambda stream: stream.write(body))
            if existing is None:
                self.metadata.publish_temp(temporary)
            else:
                temporary._inspect(sealed=True)
                # expected_sha256 comes from the durable transition, not from
                # accepting whichever file happens to be present now.
                current = self._read(name)
                if current is None or sha256(current).hexdigest() != expected_sha256:
                    raise _invalid()
                os.replace(temporary.name, name, src_dir_fd=self.metadata._fd, dst_dir_fd=self.metadata._fd)
                os.fsync(self.metadata._fd)
            self._remove_registered(temporary)
        except ArtworkError:
            raise
        except OSError:
            raise ArtworkError("RUN_STATE_WRITE_FAILED", "Unable to publish task metadata.", "download") from None

    def _remove_marker(self, body):
        current = self._read("state-transition.json")
        if current != body:
            raise _invalid()
        try:
            os.unlink("state-transition.json", dir_fd=self.metadata._fd)
            os.fsync(self.metadata._fd)
        except OSError:
            raise ArtworkError("RUN_STATE_WRITE_FAILED", "Unable to finish the task-state transaction.", "download") from None

    def _recover_state_transition(self):
        marker_body = self._read("state-transition.json")
        if marker_body is None:
            return
        marker = _document(marker_body)
        if (not isinstance(marker, dict) or type(marker.get("transition_version")) is not int
                or marker.get("transition_version") != 1
                or not _is_digest(marker.get("to_sha256"))
                or any(marker.get(key) is not None and not _is_digest(marker[key])
                       for key in ("from_sha256", "from_previous_sha256"))
                or not valid_temporary_record(marker.get("temporary"), "state.json")
                or marker["temporary"]["sealed"] is not True):
            raise _invalid()
        current = self._read("state.json")
        current_hash = sha256(current).hexdigest() if current is not None else None
        if current_hash not in {marker.get("from_sha256"), marker["to_sha256"]}:
            raise _invalid("Task-state transaction encountered an unrecognized current revision.")
        if current_hash == marker.get("from_sha256"):
            temporary = self.metadata.restore_temp(marker["temporary"])
            candidate = self._read(temporary.name)
            if candidate is None or sha256(candidate).hexdigest() != marker["to_sha256"]:
                raise _invalid()
            candidate_envelope = self._envelope(candidate)
            if candidate_envelope.get("previous_sha256") != marker.get("from_sha256"):
                raise _invalid()
            if current is not None:
                old_envelope = self._envelope(current)
                if candidate_envelope["revision"] != old_envelope["revision"] + 1:
                    raise _invalid()
                self._write_internal("state-previous.json", current,
                                     expected_sha256=marker.get("from_previous_sha256"))
            elif candidate_envelope["revision"] != 1:
                raise _invalid()
            try:
                if current is None:
                    self.metadata.publish_temp(temporary)
                else:
                    os.replace(temporary.name, "state.json", src_dir_fd=self.metadata._fd, dst_dir_fd=self.metadata._fd)
                    os.fsync(self.metadata._fd)
            except OSError:
                raise ArtworkError("RUN_STATE_WRITE_FAILED", "Unable to complete task-state publication.", "download") from None
        else:
            envelope = self._envelope(current)
            if envelope.get("previous_sha256") != marker.get("from_sha256"):
                raise _invalid()
        if marker.get("from_sha256") is not None:
            previous = self._read("state-previous.json")
            if previous is None or sha256(previous).hexdigest() != marker["from_sha256"]:
                raise _invalid()
        # The first revision is linked; subsequent revisions consume the temp.
        record = marker["temporary"]
        if self.metadata.temporary_exists(record["name"]):
            self.metadata.cleanup_temp(self.metadata.restore_temp(record))
        self._remove_marker(marker_body)

    def save(self, data=None):
        if data is not None:
            self.data = data
        if not isinstance(self.data, dict):
            raise _invalid()
        current = self._read("state.json")
        actual = sha256(current).hexdigest() if current is not None else None
        if actual != self.current_sha256 or self._read("state-transition.json") is not None:
            raise _invalid("Task state changed or has an unfinished transaction; reopen the directory to recover it.")
        if current is not None and self.loaded_version == 1:
            original = self._read("state-v1-original.json")
            if original is not None and original != current:
                raise _invalid("The pre-upgrade state backup differs from the original revision.")
            self._write_internal("state-v1-original.json", current)
            self.upgrade_from = {"sha256": sha256(current).hexdigest(), "revision": self.revision}
        envelope = {"state_version": STATE_VERSION, "revision": self.revision + 1,
                    "previous_sha256": self.current_sha256, "payload": self.data,
                    "payload_sha256": fingerprint(self.data)}
        if self.upgrade_from is not None:
            envelope["upgrade_from"] = self.upgrade_from
        body = json_bytes(envelope)
        temporary = self.metadata.allocate_temp("state.json")
        temporary.write_stream(lambda stream: stream.write(body))
        marker = {"transition_version": 1, "from_sha256": self.current_sha256,
                  "from_previous_sha256": self.previous_sha256,
                  "to_sha256": sha256(body).hexdigest(), "temporary": temporary.record()}
        # Marker is immutable while this transaction is pending. A crash before
        # it is published leaves only an unregistered file, which is retained.
        self._write_internal("state-transition.json", json_bytes(marker))
        self._recover_state_transition()
        self.previous_sha256 = self.current_sha256
        self.current_sha256 = sha256(body).hexdigest()
        self.revision += 1
        self.loaded_version = STATE_VERSION

    def _manifest_bytes(self):
        try:
            return self.output.read_file("manifest.json", max_bytes=MAX_JSON_BYTES)[0]
        except FileNotFoundError:
            return None

    @staticmethod
    def _matches(body, record):
        if body is None:
            return record is None
        return (isinstance(record, dict) and len(body) == record.get("bytes")
                and sha256(body).hexdigest() == record.get("sha256"))

    def _backup_manifest(self, body, old_record, previous_record):
        if body is None:
            return
        if not self._matches(body, old_record):
            raise _invalid()
        expected = previous_record.get("sha256") if isinstance(previous_record, dict) else None
        self._write_internal("manifest.previous.json", body, expected_sha256=expected)

    def recover_manifest(self):
        if self.data is None:
            return
        manifest_state = self.data.get("manifest")
        if (not isinstance(manifest_state, dict)
                or not {"version", "current", "previous", "temporary"} <= manifest_state.keys()
                or "manifest_pending" not in self.data or type(manifest_state.get("version")) is not int
                or manifest_state["version"] < 0
                or not valid_hash_record(manifest_state.get("current"), optional=True)
                or not valid_hash_record(manifest_state.get("previous"), optional=True)
                or (manifest_state.get("temporary") is not None
                    and (not valid_temporary_record(manifest_state["temporary"], "manifest.json")
                         or manifest_state["temporary"]["sealed"] is not True))):
            raise _invalid()
        pending = self.data.get("manifest_pending")
        current = self._manifest_bytes()
        if pending is None:
            if not self._matches(current, manifest_state.get("current")):
                raise ArtworkError("MANIFEST_CONFLICT", "The saved manifest is missing or changed; use a new output directory.", "download")
            temporary_record = manifest_state.get("temporary")
            if temporary_record is not None:
                if self.output.temporary_exists(temporary_record["name"]):
                    self.output.cleanup_temp(self.output.restore_temp(temporary_record))
                manifest_state["temporary"] = None
                self.save()
            return
        if (not isinstance(pending, dict) or not {"version", "new", "old", "temporary"} <= pending.keys()
                or not valid_hash_record(pending.get("new"))
                or not valid_hash_record(pending.get("old"), optional=True)
                or not valid_temporary_record(pending.get("temporary"), "manifest.json")
                or pending["temporary"]["sealed"] is not True
                or type(pending.get("version")) is not int
                or pending.get("old") != manifest_state.get("current")
                or pending.get("version") != manifest_state["version"] + 1):
            raise _invalid()
        if self._matches(current, pending["new"]):
            if pending.get("old") is not None:
                previous = self._read("manifest.previous.json")
                if not self._matches(previous, pending["old"]):
                    raise _invalid()
        elif self._matches(current, pending.get("old")):
            temporary = self.output.restore_temp(pending["temporary"])
            prepared = self.output.hash_file(temporary.name, max_bytes=MAX_JSON_BYTES)
            if any(prepared[key] != pending["new"].get(key) for key in ("bytes", "sha256")):
                raise _invalid()
            self._backup_manifest(current, pending.get("old"), manifest_state.get("previous"))
            self.output.publish_temp(temporary, replace=current is not None,
                                     expected_destination=pending.get("old"))
        else:
            raise ArtworkError("MANIFEST_CONFLICT", "Manifest differs from both registered transaction versions.", "download")
        self.data["manifest"] = {"version": pending["version"], "current": pending["new"],
                                 "previous": pending.get("old"), "temporary": pending["temporary"]}
        self.data["manifest_pending"] = None
        self.save()
        self.recover_manifest()

    def save_manifest(self, manifest):
        self.recover_manifest()
        body = json_bytes(manifest)
        temporary = self.output.allocate_temp("manifest.json")
        temporary.write_stream(lambda stream: stream.write(body))
        previous = self.data["manifest"]
        self.data["manifest_pending"] = {"version": previous["version"] + 1,
                                         "old": previous.get("current"),
                                         "new": {"bytes": len(body), "sha256": sha256(body).hexdigest()},
                                         "temporary": temporary.record()}
        self.save()
        try:
            self.recover_manifest()
        except ArtworkError as error:
            # Cleanup/checkpoint failure after publication must not deny the
            # already visible manifest. Its exact candidate bytes are known.
            try:
                published = self.output.hash_file("manifest.json", max_bytes=MAX_JSON_BYTES)
                committed = published["bytes"] == len(body) and published["sha256"] == sha256(body).hexdigest()
            except (ArtworkError, FileNotFoundError):
                committed = False
            raise OutputCommitError(error.code, error.message, path="manifest.json", committed=committed,
                                    residue=residue_paths(self.output), category=error.category,
                                    retryable=error.retryable, context=error.context) from None
        return {"path": "manifest.json", "status": "saved", "version": self.data["manifest"]["version"]}

    def unknown_residue(self):
        known = {"lock", "state.json", "state-previous.json", "state-transition.json", "manifest.previous.json",
                 "state-v1-original.json", "allocation.json", "diagnostics"}
        return [STATE_DIRECTORY + "/" + name for name in self.metadata.names() if name not in known]
