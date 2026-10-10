"""Library allocation is navigation, never proof of reusable media."""

from importlib.util import find_spec
from hashlib import sha256
from pathlib import Path
import tempfile
import shutil
from unittest import TestCase
from unittest.mock import patch

from apple_music_artwork.errors import ArtworkError
from apple_music_artwork.jsonio import loads_json
from apple_music_artwork.run_state import fingerprint


class Crash(BaseException):
    pass


class LibraryTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "library"
        self.identity = {"resource": {"resource_type": "albums", "resource_id": "1", "storefront": "us"},
                         "locale": "en-US", "selection": {"all_static": True}, "source": {"kind": "online"}}

    def module(self):
        self.assertIsNotNone(find_spec("apple_music_artwork.library"), "library allocation is missing")
        from apple_music_artwork import library
        return library

    def snapshot(self):
        if not self.root.exists():
            return {}
        result = {}
        for path in (self.root, *self.root.rglob("*")):
            info = path.lstat()
            result[str(path.relative_to(self.root))] = (info.st_mode, info.st_ino, info.st_mtime_ns,
                path.read_bytes() if path.is_file() and not path.is_symlink() else None)
        return result

    def pending(self):
        lib = self.module()
        with patch.object(lib, "_local_record", side_effect=Crash):
            with self.assertRaises(Crash):
                lib.allocate(self.root, self.identity, "Album", "all-static")
        return lib.resolve(self.root, fingerprint(self.identity))

    def test_readonly_lookup_never_creates_the_root(self):
        lib = self.module()
        self.assertIsNone(lib.resolve(self.root, fingerprint(self.identity)))
        self.assertEqual(lib.list_tasks(self.root), [])
        self.assertFalse(self.root.exists())

    def test_latest_explicit_allocation_survives_index_loss_and_name_change(self):
        lib = self.module()
        first = lib.allocate(self.root, self.identity, "After Hours", "all-static")
        self.assertEqual(first["version"], 1)
        self.assertIn("album/After Hours--1/us--en-US/all-static--", first["relative_path"])
        again = lib.allocate(self.root, self.identity, "Changed title", "all-static")
        self.assertEqual(again, first)
        second = lib.allocate(self.root, self.identity, "Changed title", "all-static", new_version=True)
        self.assertEqual(second["version"], 2)
        self.assertNotEqual(second["relative_path"], first["relative_path"])
        self.assertEqual(second["parent"]["version"], 1)
        index = self.root / ".apple-artwork-library" / "index.json"
        index.write_bytes(b"broken navigation index")
        latest = lib.resolve(self.root, fingerprint(self.identity))
        self.assertEqual(latest, second)
        resumed = lib.allocate(self.root, self.identity, "Third title", "all-static")
        self.assertEqual(resumed, second)
        self.assertEqual(len(lib.list_tasks(self.root)), 2)
        local = self.root / second["relative_path"] / ".apple-artwork-state" / "allocation.json"
        self.assertEqual(loads_json(local.read_bytes()), second)
        self.assertFalse((self.root / second["relative_path"] / "manifest.json").exists())

    def test_offline_changes_have_distinct_full_identity(self):
        lib = self.module()
        other = {**self.identity, "source": {"kind": "offline", "content_sha256": "a" * 64}}
        first = lib.allocate(self.root, self.identity, "Album", "all-static")
        second = lib.allocate(self.root, other, "Album", "all-static")
        self.assertEqual(first["version"], second["version"])
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertEqual(lib.resolve(self.root, first["task_id"]), first)
        self.assertEqual(lib.resolve(self.root, second["task_id"]), second)

    def test_explicit_derived_parent_survives_child_new_version_and_reuse(self):
        lib = self.module()
        parent_first = lib.allocate(self.root, self.identity, "Album", "all-static")
        child_identity = {**self.identity, "selection": {"all_static": True, "width": 2}}
        first_relation = {"task_id": parent_first["task_id"], "version": 1, "decision_id": "a" * 64}
        first = lib.allocate(self.root, child_identity, "Album", "all-static", parent=first_relation)
        parent_second = lib.allocate(self.root, self.identity, "Album", "all-static", new_version=True)
        second_relation = {"task_id": parent_second["task_id"], "version": 2, "decision_id": "b" * 64}
        second = lib.allocate(self.root, child_identity, "Album", "all-static", new_version=True,
                              parent=second_relation)
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["parent"], second_relation)
        self.assertEqual(lib.resolve(self.root, second["task_id"]), second)
        for record, relation in ((first, first_relation), (second, second_relation)):
            local = self.root / record["relative_path"] / ".apple-artwork-state" / "allocation.json"
            registry = self.root / ".apple-artwork-library" / "tasks" / record["task_id"] / f"v{record['version']}.json"
            self.assertEqual(loads_json(local.read_bytes())["parent"], relation)
            self.assertEqual(loads_json(registry.read_bytes())["parent"], relation)
        resumed = lib.allocate(self.root, child_identity, "Changed title", "all-static", parent=second_relation)
        self.assertEqual(resumed, second)
        children = [record for record in lib.list_tasks(self.root) if record["task_id"] == second["task_id"]]
        self.assertEqual(children, [first, second])

    def test_new_version_without_explicit_parent_links_to_previous_child(self):
        lib = self.module()
        relation = {"task_id": "a" * 64, "version": 3, "decision_id": "b" * 64}
        first = lib.allocate(self.root, self.identity, "Album", "all-static", parent=relation)
        second = lib.allocate(self.root, self.identity, "Album", "all-static", new_version=True)
        self.assertEqual(second["parent"], {"task_id": first["task_id"], "version": 1})
        self.assertEqual(lib.resolve(self.root, first["task_id"], version=1)["parent"], relation)
        self.assertEqual(lib.allocate(self.root, self.identity, "Changed title", "all-static"), second)
        self.assertEqual(lib.list_tasks(self.root), [first, second])

    def test_invalid_or_changed_allocation_is_never_treated_as_an_empty_library(self):
        lib = self.module()
        record = lib.allocate(self.root, self.identity, "Album", "all-static")
        local = self.root / record["relative_path"] / ".apple-artwork-state" / "allocation.json"
        local.write_bytes(b"user data")
        from apple_music_artwork.errors import ArtworkError
        with self.assertRaises(ArtworkError):
            lib.allocate(self.root, self.identity, "Album", "all-static")
        self.assertEqual(local.read_bytes(), b"user data")

    def test_expected_absence_rejects_a_concurrent_first_allocation(self):
        lib = self.module()
        first = lib.allocate(self.root, self.identity, "Album", "all-static")
        from apple_music_artwork.errors import ArtworkError
        with self.assertRaises(ArtworkError) as failure:
            lib.allocate(self.root, self.identity, "Album", "all-static", expected=None, new_version=True)
        self.assertEqual(failure.exception.code, "PLAN_STALE")
        self.assertEqual(lib.resolve(self.root, first["task_id"]), first)

    def test_long_chinese_title_is_a_readable_bounded_directory(self):
        lib = self.module()
        record = lib.allocate(self.root, self.identity, "很长的中文专辑名称 " * 30, "all-static")
        parts = Path(record["relative_path"]).parts
        self.assertIn("中文", parts[1])
        self.assertLessEqual(len(parts[1].encode("utf-8")), 240)
        self.assertTrue((self.root / record["relative_path"]).is_dir())

    def test_missing_navigation_tree_rebuilds_from_version_records_without_duplicate_versions(self):
        lib = self.module()
        first = lib.allocate(self.root, self.identity, "Album", "all-static")
        second = lib.allocate(self.root, self.identity, "Album", "all-static", new_version=True)
        shutil.rmtree(self.root / ".apple-artwork-library")
        self.assertEqual(lib.resolve(self.root, first["task_id"]), second)
        self.assertFalse((self.root / ".apple-artwork-library").exists())
        resumed = lib.allocate(self.root, self.identity, "Renamed", "all-static")
        self.assertEqual(resumed, second)
        self.assertEqual(len(lib.list_tasks(self.root)), 2)

    def test_occupied_short_label_never_becomes_identity_or_gets_overwritten(self):
        lib = self.module()
        short = fingerprint(self.identity)[:12]
        occupied = self.root / "album" / "Album--1" / "us--en-US" / f"all-static--{short}--v1"
        occupied.mkdir(parents=True)
        (occupied / "user.txt").write_bytes(b"owned by user")
        record = lib.allocate(self.root, self.identity, "Album", "all-static")
        self.assertNotEqual(self.root / record["relative_path"], occupied)
        self.assertEqual((occupied / "user.txt").read_bytes(), b"owned by user")

    def test_reserved_version_without_local_record_is_repaired_in_place(self):
        lib = self.module()
        class Crash(BaseException):
            pass
        with patch.object(lib, "_local_record", side_effect=Crash):
            with self.assertRaises(Crash):
                lib.allocate(self.root, self.identity, "Album", "all-static")
        reserved = lib.resolve(self.root, fingerprint(self.identity))
        self.assertEqual(reserved["version"], 1)
        resumed = lib.allocate(self.root, self.identity, "Changed", "all-static")
        self.assertEqual(resumed, reserved)
        self.assertTrue((self.root / reserved["relative_path"] / ".apple-artwork-state" / "allocation.json").exists())

    def test_missing_latest_registry_record_cannot_rewind_or_duplicate_a_version(self):
        lib = self.module()
        first = lib.allocate(self.root, self.identity, "Album", "all-static")
        second = lib.allocate(self.root, self.identity, "Album", "all-static", new_version=True)
        registry = self.root / ".apple-artwork-library" / "tasks" / first["task_id"]
        (registry / "v2.json").unlink()
        self.assertEqual(lib.resolve(self.root, first["task_id"]), second)
        third = lib.allocate(self.root, self.identity, "Album", "all-static", new_version=True)
        self.assertEqual(third["version"], 3)
        self.assertEqual([item["version"] for item in lib.list_tasks(self.root)], [1, 2, 3])

    def test_resource_folder_is_stable_across_title_selector_and_storefront_changes(self):
        lib = self.module()
        first = lib.allocate(self.root, self.identity, "Original title", "all-static")
        changed = {**self.identity, "selection": {"all_media": True},
                   "resource": {**self.identity["resource"], "storefront": "cn"}}
        second = lib.allocate(self.root, changed, "Changed title", "all-media")
        self.assertEqual(Path(first["relative_path"]).parts[:2], Path(second["relative_path"]).parts[:2])

    def test_new_version_completes_pending_latest_before_reserving_the_next_version(self):
        lib = self.module()
        previous = self.pending()
        registry = self.root / ".apple-artwork-library" / "tasks" / previous["task_id"]
        local = self.root / previous["relative_path"] / ".apple-artwork-state" / "allocation.json"
        before = self.snapshot()
        self.assertEqual(lib.list_tasks(self.root), [previous])
        expected_path = lib.proposed_path(self.root, self.identity, "Changed", "all-static", previous, new_version=True)
        self.assertEqual(self.snapshot(), before, "Pending allocation queries must stay read-only")
        original = lib._write
        def write(directory, name, value, **options):
            if name == "v2.json":
                self.assertTrue(local.is_file(), "The previous version must exist before a new root reservation")
                self.assertEqual(loads_json(local.read_bytes()), previous)
                self.assertEqual(loads_json((registry / "ready-v1.json").read_bytes()),
                                 {"allocation_sha256": fingerprint(previous)})
            return original(directory, name, value, **options)
        with patch.object(lib, "_write", side_effect=write):
            result = lib.allocate(self.root, self.identity, "Changed", "all-static", new_version=True,
                                  expected=previous, expected_path=expected_path)
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["parent"], {"task_id": previous["task_id"], "version": 1})
        self.assertEqual([record["version"] for record in lib.list_tasks(self.root)], [1, 2])

    def test_failure_completing_pending_latest_cannot_leave_a_nonlatest_pending_version(self):
        lib = self.module()
        previous = self.pending()
        original = lib._local_record
        def complete(root, record, *, create=False):
            if create and record["version"] == 1:
                raise Crash()
            return original(root, record, create=create)
        with patch.object(lib, "_local_record", side_effect=complete):
            with self.assertRaises(Crash):
                lib.allocate(self.root, self.identity, "Changed", "all-static", new_version=True)
        registry = self.root / ".apple-artwork-library" / "tasks" / previous["task_id"]
        self.assertFalse((registry / "v2.json").exists())
        self.assertEqual(lib.resolve(self.root, previous["task_id"]), previous)
        result = lib.allocate(self.root, self.identity, "Changed", "all-static", new_version=True)
        self.assertEqual(result["version"], 2)

    def test_pending_resource_identity_is_shared_by_a_different_task_and_storefront(self):
        lib = self.module()
        previous = self.pending()
        changed = {**self.identity, "selection": {"all_media": True},
                   "resource": {**self.identity["resource"], "storefront": "cn"}, "locale": "zh-Hans-CN"}
        before = self.snapshot()
        proposed = lib.proposed_path(self.root, changed, "Changed title", "all-media")
        self.assertEqual(Path(proposed).parts[:2], Path(previous["relative_path"]).parts[:2])
        self.assertEqual(self.snapshot(), before)
        other = lib.allocate(self.root, changed, "Changed title", "all-media")
        self.assertEqual(Path(other["relative_path"]).parts[:2], Path(previous["relative_path"]).parts[:2])
        self.assertNotEqual(Path(other["relative_path"]).parts[2], Path(previous["relative_path"]).parts[2])
        self.assertEqual(lib.allocate(self.root, self.identity, "Another name", "all-static"), previous)

    def test_missing_navigation_and_ready_receipts_are_rebuilt_only_during_allocation(self):
        lib = self.module()
        first = lib.allocate(self.root, self.identity, "Album", "all-static")
        second = lib.allocate(self.root, self.identity, "Album", "all-static", new_version=True)
        registry = self.root / ".apple-artwork-library" / "tasks" / first["task_id"]
        (registry / "v1.json").unlink()
        (registry / "v2.json").unlink()
        (registry / "ready-v2.json").unlink()
        before = self.snapshot()
        self.assertEqual(lib.resolve(self.root, first["task_id"]), second)
        self.assertEqual(lib.list_tasks(self.root), [first, second])
        self.assertEqual(self.snapshot(), before)
        third = lib.allocate(self.root, self.identity, "Album", "all-static", new_version=True, expected=second)
        self.assertEqual(third["version"], 3)
        for record in (first, second, third):
            self.assertEqual(loads_json((registry / f"v{record['version']}.json").read_bytes()), record)
            self.assertEqual(loads_json((registry / f"ready-v{record['version']}.json").read_bytes()),
                             {"allocation_sha256": fingerprint(record)})

    def test_ready_receipt_cannot_authorize_replacing_lost_or_changed_local_evidence(self):
        lib = self.module()
        record = lib.allocate(self.root, self.identity, "Album", "all-static")
        local = self.root / record["relative_path"] / ".apple-artwork-state" / "allocation.json"
        local.unlink()
        before = self.snapshot()
        for operation in (lambda: lib.resolve(self.root, record["task_id"]), lambda: lib.list_tasks(self.root),
                          lambda: lib.allocate(self.root, self.identity, "Album", "all-static", new_version=True)):
            with self.assertRaises(ArtworkError) as failure:
                operation()
            self.assertEqual(failure.exception.code, "LIBRARY_INVALID")
            self.assertEqual(self.snapshot(), before)

    def test_prior_pending_local_and_ready_publication_interruptions_never_reserve_the_next_version(self):
        lib = self.module()
        base = self.root
        for publication in ("allocation.json", "ready-v1.json"):
            for after in (False, True):
                with self.subTest(publication=publication, after=after):
                    self.root = base / f"{publication}-{after}"
                    previous = self.pending()
                    original = lib._write
                    interrupted = []
                    def write(directory, name, value, **options):
                        if name == publication and not interrupted:
                            interrupted.append(True)
                            if after:
                                original(directory, name, value, **options)
                            raise Crash()
                        return original(directory, name, value, **options)
                    with patch.object(lib, "_write", side_effect=write), self.assertRaises(Crash):
                        lib.allocate(self.root, self.identity, "Changed", "all-static", new_version=True)
                    registry = self.root / ".apple-artwork-library" / "tasks" / previous["task_id"]
                    self.assertFalse((registry / "v2.json").exists())
                    before = self.snapshot()
                    self.assertEqual(lib.resolve(self.root, previous["task_id"]), previous)
                    self.assertEqual(lib.list_tasks(self.root), [previous])
                    self.assertEqual(self.snapshot(), before)
                    result = lib.allocate(self.root, self.identity, "Changed", "all-static", new_version=True,
                                          expected=previous)
                    self.assertEqual(result["version"], 2)
                    self.assertEqual([record["version"] for record in lib.list_tasks(self.root)], [1, 2])

    def test_interrupted_index_after_new_version_never_allocates_another_on_normal_resume(self):
        lib = self.module()
        previous = self.pending()
        with patch.object(lib, "_index", side_effect=Crash), self.assertRaises(Crash):
            lib.allocate(self.root, self.identity, "Changed", "all-static", new_version=True)
        latest = lib.resolve(self.root, previous["task_id"])
        self.assertEqual(latest["version"], 2)
        self.assertEqual(lib.allocate(self.root, self.identity, "Another name", "all-static"), latest)
        self.assertEqual([record["version"] for record in lib.list_tasks(self.root)], [1, 2])

    def test_changed_ready_receipt_is_retained_and_rejected_without_repair(self):
        lib = self.module()
        record = lib.allocate(self.root, self.identity, "Album", "all-static")
        receipt = self.root / ".apple-artwork-library" / "tasks" / record["task_id"] / "ready-v1.json"
        receipt.write_bytes(b'{"allocation_sha256":"unregistered"}\n')
        before = self.snapshot()
        for operation in (lambda: lib.resolve(self.root, record["task_id"]), lambda: lib.list_tasks(self.root),
                          lambda: lib.allocate(self.root, self.identity, "Album", "all-static")):
            with self.assertRaises(ArtworkError) as failure:
                operation()
            self.assertEqual(failure.exception.code, "LIBRARY_INVALID")
            self.assertEqual(self.snapshot(), before)

    def test_unknown_short_and_full_task_directories_are_never_adopted(self):
        lib = self.module()
        task_id = fingerprint(self.identity)
        base = self.root / "album" / "Album--1" / "us--en-US"
        unknown = []
        for label in (task_id[:12], task_id):
            folder = base / f"all-static--{label}--v1"
            folder.mkdir(parents=True)
            artifact = folder / "user.txt"
            artifact.write_bytes(b"unregistered user data")
            unknown.append((artifact, artifact.stat().st_ino, artifact.read_bytes()))
        before = self.snapshot()
        with self.assertRaises(ArtworkError) as failure:
            lib.proposed_path(self.root, self.identity, "Album", "all-static")
        self.assertEqual(failure.exception.code, "OUTPUT_CONFLICT")
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(ArtworkError) as failure:
            lib.allocate(self.root, self.identity, "Album", "all-static")
        self.assertEqual(failure.exception.code, "OUTPUT_CONFLICT")
        for artifact, inode, body in unknown:
            self.assertEqual((artifact.stat().st_ino, artifact.read_bytes()), (inode, body))
        self.assertEqual(list(self.root.glob(".apple-artwork-library/tasks/*/v*.json")), [])

    def test_distinct_full_resource_ids_cannot_collapse_into_a_truncated_or_normalized_label(self):
        lib = self.module()
        for kind, ids in (("albums", ("1" * 120 + "2", "1" * 120 + "3")),
                          ("playlists", ("pl.example_", "pl.example"))):
            with self.subTest(resource_type=kind):
                records = []
                for resource_id in ids:
                    identity = {**self.identity, "resource": {**self.identity["resource"],
                                "resource_type": kind, "resource_id": resource_id}}
                    records.append(lib.allocate(self.root, identity, "Same title", "all-static"))
                folders = [Path(record["relative_path"]).parts[1] for record in records]
                self.assertNotEqual(folders[0], folders[1])
                self.assertTrue(all(len(folder.encode("utf-8")) <= 240 for folder in folders))
                for resource_id, record, folder in zip(ids, records, folders):
                    self.assertEqual(lib.resolve(self.root, record["task_id"]), record)
                    if lib.component(resource_id) != resource_id:
                        self.assertIn(sha256(resource_id.encode("utf-8")).hexdigest(), folder)

    def test_existing_allocated_resource_folder_is_kept_when_id_labels_gain_hashes(self):
        lib = self.module()
        resource_id = "2" * 120
        identity = {**self.identity, "resource": {**self.identity["resource"], "resource_id": resource_id}}
        # Seed a valid older allocation whose label used the original display
        # truncation. Its persisted identity remains the full resource ID.
        with patch.object(lib, "_resource_label", side_effect=lib.component, create=True):
            previous = lib.allocate(self.root, identity, "Original title", "all-static")
        before = self.snapshot()
        self.assertEqual(lib.resolve(self.root, previous["task_id"]), previous)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(lib.allocate(self.root, identity, "Renamed title", "all-static"), previous)
        other_identity = {**identity, "selection": {"all_media": True}}
        other = lib.allocate(self.root, other_identity, "Renamed again", "all-media")
        self.assertEqual(Path(previous["relative_path"]).parts[:2], Path(other["relative_path"]).parts[:2])
