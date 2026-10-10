"""Inject crashes around registered state and manifest publication boundaries."""

from hashlib import sha256
from pathlib import Path
import os
import subprocess
import sys
import tempfile
from unittest import TestCase, skipUnless
from unittest.mock import patch

from apple_music_artwork.download import OutputDirectory
from apple_music_artwork.errors import ArtworkError
from apple_music_artwork.jsonio import dumps_json, loads_json
from apple_music_artwork.run_state import RunState, STATE_DIRECTORY, fingerprint, residue_paths


class Crash(BaseException):
    pass


def initial():
    return {"task_identity": {"resource_id": "synthetic"}, "files": {},
            "manifest": {"version": 0, "current": None, "previous": None, "temporary": None},
            "manifest_pending": None}


@skipUnless(os.name == "posix", "Task locking requires POSIX")
class RunStateTests(TestCase):
    @classmethod
    def setUpClass(cls):
        runtime = Path(tempfile.gettempdir()).resolve()
        assert runtime.resolve(strict=True) == runtime
        cls.scope = Path(tempfile.mkdtemp(prefix="v03-core-state-", dir=runtime))

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="case-", dir=self.scope))

    def test_locked_directory_refuses_a_second_open_without_removing_lock(self):
        with OutputDirectory(self.folder) as first, OutputDirectory(self.folder) as second:
            with RunState(first):
                with self.assertRaises(ArtworkError) as failure:
                    with RunState(second):
                        pass
                self.assertEqual(failure.exception.code, "OUTPUT_BUSY")
                self.assertTrue((self.folder / STATE_DIRECTORY / "lock").is_file())
            with RunState(second) as reopened:
                self.assertIsNone(reopened.data)

    def test_state_revisions_preserve_exact_previous_document(self):
        with OutputDirectory(self.folder) as output, RunState(output) as state:
            state.save(initial())
            first = (self.folder / STATE_DIRECTORY / "state.json").read_bytes()
            state.data["files"]["one"] = {"status": "pending"}
            state.save()
            self.assertEqual((self.folder / STATE_DIRECTORY / "state-previous.json").read_bytes(), first)
            self.assertEqual(state.revision, 2)
        with OutputDirectory(self.folder) as output, RunState(output) as state:
            self.assertEqual(state.data["files"]["one"]["status"], "pending")
            self.assertEqual(state.unknown_residue(), [])

    def test_actual_child_process_contends_then_reuses_the_same_lock_inode(self):
        code = """import fcntl,os,sys
fd=os.open(sys.argv[1],os.O_RDWR|os.O_NOFOLLOW)
print(os.fstat(fd).st_ino,flush=True)
try:
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(6)
finally:
    os.close(fd)
"""
        lock = self.folder / STATE_DIRECTORY / "lock"
        with OutputDirectory(self.folder) as output:
            with RunState(output):
                inode = lock.stat().st_ino
                busy = subprocess.run([sys.executable, "-B", "-c", code, str(lock)],
                                      capture_output=True, text=True, timeout=10)
                self.assertEqual(busy.returncode, 6)
                self.assertEqual(int(busy.stdout.strip()), inode)
            acquired = subprocess.run([sys.executable, "-B", "-c", code, str(lock)],
                                      capture_output=True, text=True, timeout=10)
            self.assertEqual(acquired.returncode, 0)
            self.assertEqual(int(acquired.stdout.strip()), inode)
            self.assertEqual(lock.stat().st_ino, inode)

    def test_state_recovers_marker_before_atomic_replace(self):
        with OutputDirectory(self.folder) as output, RunState(output) as state:
            state.save(initial())
            state.data["files"]["one"] = {"status": "prepared"}
            real_replace = os.replace

            def interrupted(source, destination, **kwargs):
                if destination == "state.json":
                    raise Crash()
                return real_replace(source, destination, **kwargs)

            with patch("apple_music_artwork.run_state.os.replace", side_effect=interrupted):
                with self.assertRaises(Crash):
                    state.save()
        with OutputDirectory(self.folder) as output, RunState(output) as state:
            self.assertEqual(state.data["files"]["one"]["status"], "prepared")
            self.assertEqual(state.revision, 2)
            self.assertFalse((self.folder / STATE_DIRECTORY / "state-transition.json").exists())

    def test_state_recovers_after_publish_before_marker_removal(self):
        with OutputDirectory(self.folder) as output, RunState(output) as state:
            with patch.object(state, "_remove_marker", side_effect=Crash):
                with self.assertRaises(Crash):
                    state.save(initial())
        with OutputDirectory(self.folder) as output, RunState(output) as state:
            self.assertEqual(state.data, initial())
            self.assertEqual(state.revision, 1)
            self.assertEqual(state.unknown_residue(), [])

    def test_corrupt_state_or_unrecognized_current_revision_is_not_replaced(self):
        with OutputDirectory(self.folder) as output, RunState(output) as state:
            state.save(initial())
        path = self.folder / STATE_DIRECTORY / "state.json"
        damaged = b'{"state_version":1,"state_version":1}\n'
        path.write_bytes(damaged)
        with OutputDirectory(self.folder) as output:
            with self.assertRaises(ArtworkError) as failure:
                with RunState(output):
                    pass
        self.assertEqual(failure.exception.code, "RUN_STATE_INVALID")
        self.assertEqual(path.read_bytes(), damaged)

    def test_manifest_before_publication_is_recovered_from_registered_candidate(self):
        manifest = {"schema_version": 2, "status": "success", "downloads": [{"path": "synthetic.png"}]}
        with OutputDirectory(self.folder) as output, RunState(output) as state:
            state.save(initial())
            with patch.object(output, "publish_temp", side_effect=Crash):
                with self.assertRaises(Crash):
                    state.save_manifest(manifest)
            self.assertFalse((self.folder / "manifest.json").exists())
        with OutputDirectory(self.folder) as output, RunState(output) as state:
            state.recover_manifest()
            self.assertEqual(loads_json((self.folder / "manifest.json").read_bytes()), manifest)
            self.assertIsNone(state.data["manifest_pending"])

    def test_manifest_published_before_checkpoint_is_accepted_and_previous_retained(self):
        first, second = {"version": 1}, {"version": 2}
        with OutputDirectory(self.folder) as output, RunState(output) as state:
            state.save(initial())
            state.save_manifest(first)
            old = (self.folder / "manifest.json").read_bytes()
            real_save = state.save

            def interrupted(data=None):
                if state.data.get("manifest_pending") is None and state.data["manifest"]["version"] == 2:
                    raise Crash()
                return real_save(data)

            with patch.object(state, "save", side_effect=interrupted):
                with self.assertRaises(Crash):
                    state.save_manifest(second)
            self.assertEqual(loads_json((self.folder / "manifest.json").read_bytes()), second)
        with OutputDirectory(self.folder) as output, RunState(output) as state:
            state.recover_manifest()
            self.assertEqual(loads_json((self.folder / "manifest.json").read_bytes()), second)
            self.assertEqual((self.folder / STATE_DIRECTORY / "manifest.previous.json").read_bytes(), old)
            self.assertEqual(state.data["manifest"]["version"], 2)
            self.assertEqual(list(self.folder.glob(".apple-artwork-*.part")), [])

    def test_external_manifest_change_is_preserved_as_conflict(self):
        with OutputDirectory(self.folder) as output, RunState(output) as state:
            state.save(initial())
            state.save_manifest({"version": 1})
        changed = b"external data must survive"
        (self.folder / "manifest.json").write_bytes(changed)
        with OutputDirectory(self.folder) as output, RunState(output) as state:
            with self.assertRaises(ArtworkError) as failure:
                state.recover_manifest()
            self.assertEqual(failure.exception.code, "MANIFEST_CONFLICT")
        self.assertEqual((self.folder / "manifest.json").read_bytes(), changed)

    def test_manifest_wrap_preserves_recovery_context_and_retryability(self):
        error = ArtworkError("RUN_STATE_WRITE_FAILED", "Synthetic state failure.", "download", True,
                             context={"reason": "local_environment", "scope": "task", "operation": "state"})
        with OutputDirectory(self.folder) as output, RunState(output) as state:
            state.save(initial())
            with patch.object(state, "recover_manifest", side_effect=[None, error]):
                with self.assertRaises(ArtworkError) as failure:
                    state.save_manifest({"version": 1})
            self.assertEqual(failure.exception.to_dict(), error.to_dict())

    def test_task_fingerprint_has_stable_object_order_and_strict_json(self):
        self.assertEqual(fingerprint({"id": "1", "locale": "en-US"}), fingerprint({"locale": "en-US", "id": "1"}))
        with self.assertRaises(ArtworkError):
            fingerprint({"invalid": float("nan")})

    def test_unregistered_residue_is_reported_without_deletion(self):
        with OutputDirectory(self.folder) as output, RunState(output) as state:
            state.save(initial())
            residue = self.folder / STATE_DIRECTORY / ".apple-artwork-" # unrelated name
            residue.write_bytes(b"do not remove")
            self.assertIn(STATE_DIRECTORY + "/" + residue.name, state.unknown_residue())
            state.save()
            self.assertEqual(residue.read_bytes(), b"do not remove")

    def test_malformed_manifest_and_temporary_records_are_structured_errors(self):
        for change in ({"current": {}}, {"temporary": {}}, {"previous": {"sha256": "wrong"}},
                       {"temporary": {"final_name": "manifest.json"}}):
            folder = Path(tempfile.mkdtemp(prefix="record-", dir=self.folder))
            with OutputDirectory(folder) as output, RunState(output) as state:
                data = initial()
                data["manifest"].update(change)
                state.save(data)
                with self.assertRaises(ArtworkError) as failure:
                    state.recover_manifest()
                self.assertEqual(failure.exception.code, "RUN_STATE_INVALID")
        folder = Path(tempfile.mkdtemp(prefix="pending-", dir=self.folder))
        with OutputDirectory(folder) as output, RunState(output) as state:
            data = initial()
            data["manifest_pending"] = {"version": 1, "new": {"bytes": 2, "sha256": "a" * 64}, "temporary": {}}
            state.save(data)
            with self.assertRaises(ArtworkError) as failure:
                state.recover_manifest()
            self.assertEqual(failure.exception.code, "RUN_STATE_INVALID")

    def test_unregistered_allocation_and_marker_failure_are_retained_and_reported(self):
        with OutputDirectory(self.folder) as output:
            with patch("apple_music_artwork.download.os.fsync", side_effect=OSError("injected")):
                with self.assertRaises(ArtworkError):
                    output.allocate_temp("cover.png")
            before = residue_paths(output)
            self.assertEqual(len(before), 1)
            with RunState(output) as state:
                with patch.object(state, "_write_internal", side_effect=ArtworkError("RUN_STATE_WRITE_FAILED", "injected", "download")):
                    with self.assertRaises(ArtworkError):
                        state.save(initial())
                residue = residue_paths(output)
                self.assertEqual(len(residue), 2)
                self.assertTrue(all((self.folder / name).is_file() for name in residue))

    def test_boolean_versions_are_not_accepted_as_integer_schema_versions(self):
        for transition in (False, True):
            folder = Path(tempfile.mkdtemp(prefix="boolean-version-", dir=self.folder))
            with OutputDirectory(folder) as output, RunState(output) as state:
                if transition:
                    with patch.object(state, "_recover_state_transition", side_effect=Crash):
                        with self.assertRaises(Crash):
                            state.save(initial())
                else:
                    state.save(initial())
            name, field = ("state-transition.json", "transition_version") if transition else ("state.json", "state_version")
            path = folder / STATE_DIRECTORY / name
            value = loads_json(path.read_bytes())
            value[field] = True
            body = dumps_json(value).encode()
            path.write_bytes(body)
            with OutputDirectory(folder) as output:
                with self.assertRaises(ArtworkError) as failure:
                    with RunState(output):
                        pass
                self.assertEqual(failure.exception.code, "RUN_STATE_INVALID")
            self.assertEqual(path.read_bytes(), body)
