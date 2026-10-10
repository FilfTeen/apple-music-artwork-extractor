"""Read-only queries and independent pre-upgrade proof, using synthetic state."""

from pathlib import Path
from hashlib import sha256
import tempfile
from unittest import TestCase
from unittest.mock import patch

from apple_music_artwork.download import OutputDirectory
from apple_music_artwork.errors import ArtworkError
from apple_music_artwork.jsonio import loads_json
from apple_music_artwork.run_state import RunState, STATE_DIRECTORY, json_bytes, fingerprint


class StateV04Tests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_readonly_missing_directory_never_creates_it(self):
        target = self.root / "absent" / "nested"
        self.assertTrue(hasattr(OutputDirectory, "child"), "safe child directory support is missing")
        with self.assertRaises(FileNotFoundError):
            with OutputDirectory(target, create=False):
                pass
        self.assertFalse((self.root / "absent").exists())

    def test_child_rejects_symlinks_and_traversal_without_widening_leaf_names(self):
        self.assertTrue(hasattr(OutputDirectory, "child"), "safe child directory support is missing")
        outside = self.root / "outside"
        outside.mkdir()
        with OutputDirectory(self.root / "task") as task:
            with task.child("images", create=True) as images:
                images.write_bytes("cover.png", b"synthetic")
                with self.assertRaises(ArtworkError):
                    images.write_bytes("nested/cover.png", b"bad")
            (self.root / "task" / "videos").symlink_to(outside, target_is_directory=True)
            for name in ("videos", "../outside", "images/nested"):
                with self.assertRaises(ArtworkError):
                    with task.child(name, create=True):
                        pass
        self.assertEqual(list(outside.iterdir()), [])

    def test_peek_preserves_v1_then_upgrade_keeps_original_across_revisions(self):
        self.assertTrue(hasattr(RunState, "peek"), "read-only state inspection is missing")
        payload = {"files": {}, "manifest": {"version": 0, "current": None, "previous": None,
                   "temporary": None}, "manifest_pending": None}
        original = json_bytes({"state_version": 1, "revision": 1, "previous_sha256": None,
                               "payload": payload, "payload_sha256": fingerprint(payload)})
        metadata = self.root / STATE_DIRECTORY
        metadata.mkdir()
        (metadata / "state.json").write_bytes(original)
        before = sorted(path.name for path in metadata.iterdir())
        with OutputDirectory(self.root, create=False) as output:
            snapshot = RunState.peek(output)
            self.assertEqual(snapshot["state_version"], 1)
        self.assertEqual(sorted(path.name for path in metadata.iterdir()), before)
        with OutputDirectory(self.root) as output, RunState(output) as state:
            state.save()
            state.data["extra"] = "later"
            state.save()
            state.save()
        self.assertEqual((metadata / "state-v1-original.json").read_bytes(), original)
        self.assertEqual(loads_json((metadata / "state.json").read_bytes())["state_version"], 2)
        with OutputDirectory(self.root, create=False) as output:
            self.assertEqual(RunState.peek(output)["payload"]["extra"], "later")

    def test_peek_does_not_recover_a_pending_transaction(self):
        self.assertTrue(hasattr(RunState, "peek"), "read-only state inspection is missing")
        metadata = self.root / STATE_DIRECTORY
        metadata.mkdir()
        marker = b'{"synthetic":"unfinished"}'
        (metadata / "state-transition.json").write_bytes(marker)
        with OutputDirectory(self.root, create=False) as output:
            with self.assertRaises(ArtworkError) as failure:
                RunState.peek(output)
        self.assertEqual(failure.exception.code, "RUN_STATE_RECOVERY_REQUIRED")
        self.assertEqual((metadata / "state-transition.json").read_bytes(), marker)

    def test_independent_backup_cannot_be_unregistered_or_replaced(self):
        for modification in ("unregister", "replace"):
            target = self.root / modification
            metadata = target / STATE_DIRECTORY
            metadata.mkdir(parents=True)
            original = json_bytes({"state_version": 1, "revision": 1, "previous_sha256": None,
                                   "payload": {}, "payload_sha256": fingerprint({})})
            (metadata / "state.json").write_bytes(original)
            with OutputDirectory(target) as output, RunState(output) as state:
                state.save()
                state.save()
            if modification == "replace":
                (metadata / "state-v1-original.json").write_bytes(b"changed original")
            else:
                value = loads_json((metadata / "state.json").read_bytes())
                value.pop("upgrade_from")
                (metadata / "state.json").write_bytes(json_bytes(value))
            with OutputDirectory(target) as output:
                with self.assertRaises(ArtworkError) as failure:
                    RunState.peek(output)
                self.assertEqual(failure.exception.code, "RUN_STATE_INVALID")

    def test_upgrade_recovers_at_each_publication_boundary(self):
        class Crash(BaseException):
            pass

        for phase in ("backup", "marker", "replace", "cleanup"):
            with self.subTest(phase=phase):
                target = self.root / phase
                metadata = target / STATE_DIRECTORY
                metadata.mkdir(parents=True)
                original = json_bytes({"state_version": 1, "revision": 1, "previous_sha256": None,
                                       "payload": {}, "payload_sha256": fingerprint({})})
                (metadata / "state.json").write_bytes(original)
                with OutputDirectory(target) as output, RunState(output) as state:
                    real_write, real_recover = state._write_internal, state._recover_state_transition
                    def write(name, body, **kwargs):
                        real_write(name, body, **kwargs)
                        if (phase == "backup" and name == "state-v1-original.json") or (phase == "marker" and name == "state-transition.json"):
                            raise Crash()
                    def recover():
                        if phase == "replace":
                            raise Crash()
                        return real_recover()
                    with patch.object(state, "_write_internal", write), patch.object(state, "_recover_state_transition", recover), \
                         patch.object(state, "_remove_marker", side_effect=Crash if phase == "cleanup" else state._remove_marker):
                        with self.assertRaises(Crash):
                            state.save()
                with OutputDirectory(target) as output, RunState(output) as state:
                    state.save()
                    state.save()
                self.assertEqual((metadata / "state-v1-original.json").read_bytes(), original)
                self.assertEqual(loads_json((metadata / "state.json").read_bytes())["state_version"], 2)

    def test_peek_returns_payload_and_digest_from_one_verified_read(self):
        with OutputDirectory(self.root) as output, RunState(output) as state:
            state.save({"observation": "first"})
        original = (self.root / STATE_DIRECTORY / "state.json").read_bytes()
        changed = loads_json(original)
        changed["payload"] = {"observation": "second"}
        changed["payload_sha256"] = fingerprint(changed["payload"])
        real_read, count = RunState._read, 0
        def read(state, name):
            nonlocal count
            if name == "state.json":
                count += 1
                if count > 1:
                    return json_bytes(changed)
            return real_read(state, name)
        with OutputDirectory(self.root, create=False) as output, patch.object(RunState, "_read", read):
            result = RunState.peek(output)
        self.assertEqual(result["payload"], {"observation": "first"})
        self.assertEqual(result["sha256"], sha256(original).hexdigest())

    def test_upgrade_proof_revision_must_match_independent_original(self):
        metadata = self.root / STATE_DIRECTORY
        metadata.mkdir()
        original = json_bytes({"state_version": 1, "revision": 1, "previous_sha256": None,
                               "payload": {}, "payload_sha256": fingerprint({})})
        (metadata / "state.json").write_bytes(original)
        with OutputDirectory(self.root) as output, RunState(output) as state:
            state.save()
        value = loads_json((metadata / "state.json").read_bytes())
        value["upgrade_from"]["revision"] = True
        (metadata / "state.json").write_bytes(json_bytes(value))
        with OutputDirectory(self.root, create=False) as output:
            with self.assertRaises(ArtworkError):
                RunState.peek(output)
