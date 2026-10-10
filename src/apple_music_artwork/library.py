"""Bounded library navigation and durable, explicit version allocation.

The root lock covers allocation records and the disposable navigation index only.
It is always released before a task lock is acquired. Media proofs live in tasks.
"""

from contextlib import ExitStack, contextmanager
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
import unicodedata

from .download import OutputDirectory
from .errors import ArtworkError
from .jsonio import MAX_JSON_BYTES, loads_json
from .run_state import RunState, STATE_DIRECTORY, fingerprint, json_bytes, utc_now


LIBRARY_DIRECTORY = ".apple-artwork-library"
MAX_TASKS = 4096
MAX_VERSIONS = 4096
_DIGEST = re.compile(r"[0-9a-f]{64}")
_UNSET = object()


def _invalid():
    return ArtworkError("LIBRARY_INVALID", "Library allocation records cannot be trusted; inspect the retained records.", "download")


def component(value, fallback="resource"):
    text = unicodedata.normalize("NFC", str(value or fallback))
    text = "".join(char if (char.isalnum() or char in " ._-") else "-" for char in text)
    text = re.sub(r"\s+", " ", text).strip(" ._-") or fallback
    while len(text.encode("utf-8")) > 100:
        text = text[:-1]
    return text


def _resource_label(resource_id):
    raw = str(resource_id)
    label = component(raw)
    if label == raw:
        return label
    # IDs may be longer than readable path components or end in normalized
    # punctuation. Preserve their complete identity without exceeding the
    # directory-component bound; existing allocations retain their old path.
    while len(label.encode("utf-8")) > 60:
        label = label[:-1]
    return label + "--" + sha256(raw.encode("utf-8")).hexdigest()


@contextmanager
def open_relative(root, relative, *, create=False):
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(part in {".", ".."} for part in parts):
        raise _invalid()
    with ExitStack() as stack:
        current = root
        for part in parts:
            current = stack.enter_context(current.child(part, create=create))
        yield current


def _read(directory, name):
    try:
        return directory.read_file(name, max_bytes=MAX_JSON_BYTES)[0]
    except FileNotFoundError:
        return None


def _write(directory, name, value, *, previous=None):
    state = RunState(directory)
    state.metadata = directory
    state._write_internal(name, json_bytes(value), expected_sha256=sha256(previous).hexdigest() if previous else None)


def _validate(record, task_id):
    try:
        if (not isinstance(record, dict) or record["allocation_version"] != 1
                or type(record["version"]) is not int or not 1 <= record["version"] <= MAX_VERSIONS
                or record["task_id"] != task_id or fingerprint(record["identity"]) != task_id
                or record["layout_version"] != 2 or len(Path(record["relative_path"]).parts) != 4
                or Path(record["relative_path"]).is_absolute()
                or any(part in {".", ".."} for part in Path(record["relative_path"]).parts)):
            raise _invalid()
        return record
    except (KeyError, TypeError, ValueError):
        raise _invalid() from None


def _records(task_directory, task_id):
    names = task_directory.names()
    if len(names) > MAX_VERSIONS * 2:
        raise _invalid()
    records = []
    for name in names:
        if re.fullmatch(r"ready-v[1-9][0-9]{0,3}\.json", name):
            continue
        if not re.fullmatch(r"v[1-9][0-9]{0,3}\.json", name):
            raise _invalid()
        try:
            record = _validate(loads_json(_read(task_directory, name)), task_id)
        except ArtworkError:
            raise _invalid() from None
        if name != f"v{record['version']}.json":
            raise _invalid()
        records.append(record)
    records.sort(key=lambda record: record["version"])
    return records


def _local_record(root, record, *, create=False):
    with open_relative(root, record["relative_path"], create=create) as version:
        with version.child(STATE_DIRECTORY, create=create) as metadata:
            body = _read(metadata, "allocation.json")
            if body is None and create:
                if set(version.names()) - {STATE_DIRECTORY} or metadata.names():
                    raise _invalid()
                _write(metadata, "allocation.json", record)
            elif body is None:
                raise FileNotFoundError("Allocation has not been completed")
            elif loads_json(body) != record:
                raise _invalid()


def _scan_allocations(root, task_id=None):
    """Read version facts only inside this root and fixed directory depth 4."""
    found, visited = [], 0

    def visit(directory, parts):
        nonlocal visited
        visited += 1
        if visited > MAX_TASKS * 4:
            raise ArtworkError("LIBRARY_SCAN_LIMIT", "Library recovery exceeds the bounded directory scan limit.", "download")
        if len(parts) == 4:
            try:
                with directory.child(STATE_DIRECTORY) as metadata:
                    body = _read(metadata, "allocation.json")
                if body is None:
                    return
                value = loads_json(body)
                record = _validate(value, value.get("task_id"))
                if record["relative_path"] != str(Path(*parts)):
                    raise _invalid()
                if task_id is None or record["task_id"] == task_id:
                    found.append(record)
                if len(found) > MAX_TASKS:
                    raise _invalid()
            except FileNotFoundError:
                return
            return
        names = directory.names()
        if len(names) > MAX_TASKS:
            raise _invalid()
        for name in names:
            if not parts and name not in {"album", "artist", "playlist", "song"}:
                continue
            if parts and (not name or name.startswith(".")):
                continue
            # Only descend directory entries; never follow symbolic links.
            info = os.stat(name, dir_fd=directory._fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise _invalid()
            if not stat.S_ISDIR(info.st_mode):
                continue
            with directory.child(name) as child:
                visit(child, [*parts, name])

    visit(root, [])
    identities = set()
    for record in found:
        key = record["task_id"], record["version"]
        if key in identities:
            raise _invalid()
        identities.add(key)
    return sorted(found, key=lambda record: (record["task_id"], record["version"]))


def _reconcile(root, task_id, *, authoritative=None):
    authoritative = (_scan_allocations(root, task_id) if authoritative is None else
                     [record for record in authoritative if record["task_id"] == task_id])
    versions = {record["version"]: record for record in authoritative}
    authoritative_versions = set(versions)
    registered, ready = [], set()
    try:
        with root.child(LIBRARY_DIRECTORY) as metadata, metadata.child("tasks") as tasks, tasks.child(task_id) as task:
            registered = _records(task, task_id)
            for record in registered:
                previous = versions.get(record["version"])
                if previous is not None and previous != record:
                    raise _invalid()
                versions[record["version"]] = record
            for name in task.names():
                match = re.fullmatch(r"ready-v([1-9][0-9]{0,3})\.json", name)
                if match is None:
                    continue
                number = int(match[1])
                receipt = loads_json(_read(task, name))
                if (number not in versions or receipt != {"allocation_sha256": fingerprint(versions[number])}
                        or number not in authoritative_versions):
                    raise _invalid()
                ready.add(number)
    except FileNotFoundError:
        pass
    records = sorted(versions.values(), key=lambda record: record["version"])
    if [record["version"] for record in records] != list(range(1, len(records) + 1)):
        raise _invalid()
    for record in records:
        if record["version"] in authoritative_versions:
            _local_record(root, record)
        elif record != records[-1] or record["version"] in ready:
            raise _invalid()
    return records, authoritative_versions


def resolve(root_path, task_id, *, version=None):
    """Reconcile one full task identity with bounded local facts, without writes."""
    if not isinstance(task_id, str) or _DIGEST.fullmatch(task_id) is None:
        raise ArtworkError("INVALID_TASK_REFERENCE", "Use the complete task fingerprint, optionally followed by @version.", "input")
    try:
        with OutputDirectory(root_path, create=False) as root:
            records, complete = _reconcile(root, task_id)
            if not records:
                return None
            result = records[-1] if version is None else next((item for item in records if item["version"] == version), None)
            return result
    except FileNotFoundError:
        return None


def list_tasks(root_path):
    try:
        with OutputDirectory(root_path, create=False) as root:
            authoritative = _scan_allocations(root)
            by_task = {}
            for record in authoritative:
                by_task.setdefault(record["task_id"], []).append(record)
            ids = set(by_task)
            try:
                with root.child(LIBRARY_DIRECTORY) as metadata, metadata.child("tasks") as tasks:
                    ids.update(tasks.names())
            except FileNotFoundError:
                pass
            if len(ids) > MAX_TASKS or any(_DIGEST.fullmatch(name) is None for name in ids):
                raise _invalid()
            return [record for task_id in sorted(ids)
                    for record in _reconcile(root, task_id, authoritative=by_task.get(task_id, ()))[0]]
    except FileNotFoundError:
        return []


def _index(metadata, records):
    old = _read(metadata, "index.json")
    index = {"index_version": 1, "tasks": {}}
    if old is not None:
        try:
            candidate = loads_json(old)
            if not isinstance(candidate, dict) or candidate.get("index_version") != 1 or not isinstance(candidate.get("tasks"), dict):
                raise _invalid()
            index = candidate
        except ArtworkError:
            # A damaged navigation cache is retained independently. It never
            # authorizes allocating a new version or reusing media.
            backup = "index-rejected-" + sha256(old).hexdigest() + ".json"
            if _read(metadata, backup) is None:
                metadata.write_bytes(backup, old)
    latest = records[-1]
    index["tasks"][latest["task_id"]] = {"version": latest["version"], "relative_path": latest["relative_path"]}
    _write(metadata, "index.json", index, previous=old)


def proposed_path(root_path, identity, resource_name, selection, latest=None, *, new_version=False):
    if latest is not None and not new_version:
        return latest["relative_path"]
    task_id = fingerprint(identity)
    version = latest["version"] + 1 if latest else 1
    key = identity["resource"]
    resource_folder = Path(component(key["resource_type"].removesuffix("s"))) / (component(resource_name) + "--" + _resource_label(key["resource_id"]))
    if latest is None:
        # A durable reservation already identifies its resource folder, even
        # when interruption prevented the in-version record from being written.
        related = [record for record in list_tasks(root_path)
                   if all(record["identity"]["resource"][field] == key[field]
                          for field in ("resource_type", "resource_id"))]
        folders = {Path(*Path(record["relative_path"]).parts[:2]) for record in related}
        if len(folders) > 1:
            raise _invalid()
        if folders:
            resource_folder = next(iter(folders))
    base = (Path(latest["relative_path"]).parent if latest else resource_folder /
            (component(key["storefront"]) + "--" + component(identity.get("locale"), "unspecified")))
    relative = str(base / f"{component(selection)}--{task_id[:12]}--v{version}")
    try:
        with OutputDirectory(root_path, create=False) as root, open_relative(root, relative):
            relative = str(base / f"{component(selection)}--{task_id}--v{version}")
        with OutputDirectory(root_path, create=False) as root, open_relative(root, relative):
            raise ArtworkError("OUTPUT_CONFLICT", "Both proposed version directories are occupied; their contents were retained.", "download")
    except FileNotFoundError:
        pass
    return relative


def allocate(root_path, identity, resource_name, selection, *, new_version=False, expected=_UNSET,
             expected_path=None, parent=None):
    """Commit allocation before updating navigation; root lock never nests a task lock."""
    import fcntl
    task_id = fingerprint(identity)
    with OutputDirectory(root_path) as root, root.child(LIBRARY_DIRECTORY, create=True) as metadata:
        lock = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=metadata._fd)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise _invalid()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ArtworkError("LIBRARY_BUSY", "Another process is allocating a library version; retry shortly.", "download") from None
            with metadata.child("tasks", create=True) as tasks, tasks.child(task_id, create=True) as task:
                records, complete = _reconcile(root, task_id)
                for record in records:
                    _write(task, f"v{record['version']}.json", record)
                    if record["version"] in complete:
                        _write(task, f"ready-v{record['version']}.json", {"allocation_sha256": fingerprint(record)})
                latest = records[-1] if records else None
                if expected is not _UNSET and expected != latest:
                    raise ArtworkError("PLAN_STALE", "The latest library allocation changed; inspect a new plan.", "input")
                if latest is not None and latest["version"] not in complete:
                    # Finish the already authorized reservation before a newer
                    # one can exist; otherwise it becomes an invalid nonlatest
                    # pending record that neither lookup nor resume can repair.
                    _local_record(root, latest, create=True)
                    _write(task, f"ready-v{latest['version']}.json", {"allocation_sha256": fingerprint(latest)})
                if latest is not None and not new_version:
                    _index(metadata, records)
                    return latest
                if len(records) >= MAX_VERSIONS:
                    raise _invalid()
                version = len(records) + 1
                relative = proposed_path(root_path, identity, resource_name, selection, latest, new_version=new_version)
                if expected_path is not None and relative != expected_path:
                    raise ArtworkError("PLAN_STALE", "The proposed version directory changed; inspect a new plan.", "input")
                record = {"allocation_version": 1, "task_id": task_id, "identity": identity,
                          "version": version, "layout_version": 2, "relative_path": relative,
                          "created_at": utc_now(), "parent": (parent if parent is not None else
                              {"task_id": task_id, "version": latest["version"]} if latest else None)}
                # The immutable root record is the crash-recovery authority.
                # The matching in-version record is required before execution.
                _write(task, f"v{version}.json", record)
                _local_record(root, record, create=True)
                _write(task, f"ready-v{version}.json", {"allocation_sha256": fingerprint(record)})
                records.append(record)
                _index(metadata, records)
                return record
        finally:
            os.close(lock)
