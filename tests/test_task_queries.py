"""Synthetic local task navigation and selection admission; sockets are forbidden."""

from argparse import Namespace
from copy import deepcopy
from importlib.util import find_spec
import os
import stat
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from apple_music_artwork.client import HttpClient
from apple_music_artwork.download import OutputDirectory
from apple_music_artwork.jsonio import dumps_json, loads_json
from apple_music_artwork.run_state import RunState, fingerprint
from apple_music_artwork.workflow import dispatch
from test_execution import Images, NoNetwork, response


class TaskQueryTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("offline test attempted a socket operation"))
            guard.start()
            self.addCleanup(guard.stop)

    def create(self, resource_id="1", *, new_version=False, bad=False, name="Same Album"):
        body = response()
        body["data"][0].update(id=resource_id, href=f"/v1/catalog/us/albums/{resource_id}")
        body["data"][0]["attributes"]["name"] = name
        source = self.root / (resource_id + ".json")
        source.write_text(dumps_json(body), encoding="utf-8")
        args = Namespace(command="download", url=f"https://music.apple.com/us/album/example/{resource_id}?l=en-US",
                         response=str(source), library=str(self.library), all_static=True,
                         new_version=new_version, json=True, no_input=True, progress="never")
        result, code = dispatch(args, http=HttpClient(Images(bad={"synthetic-0"} if bad else ())), environ={})
        self.assertEqual(code, 8 if bad else 0, result)
        return result["control"]["resume"]["task_ref"]

    def query(self, **options):
        self.assertIsNotNone(find_spec("apple_music_artwork.task_queries"), "shared read-only task query is missing")
        from apple_music_artwork.task_queries import query_tasks
        return query_tasks(self.library, **options)

    def listing(self, **options):
        result, code = dispatch(Namespace(command="tasks.list", library=str(self.library), **options),
                                http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 0, result)
        return result["data"]

    def task_path(self, reference):
        from apple_music_artwork.library import resolve
        task_id, version = reference.split("@")
        return self.library / resolve(self.library, task_id, version=int(version))["relative_path"]

    def tree(self, root=None):
        root = root or self.library
        return {str(path.relative_to(root)): ("dir" if path.is_dir() else path.read_bytes() if path.is_file()
                                            else stat.S_IFMT(path.lstat().st_mode))
                for path in root.rglob("*")} if root.exists() else {}

    def rewrite_state(self, path, change):
        state_file = path / ".apple-artwork-state" / "state.json"
        value = loads_json(state_file.read_bytes())
        change(value["payload"])
        value["payload_sha256"] = fingerprint(value["payload"])
        state_file.write_text(dumps_json(value), encoding="utf-8")

    def test_list_exposes_readable_facts_without_public_binding_or_writes(self):
        reference = self.create()
        before = self.tree()
        with patch("fcntl.flock", side_effect=AssertionError("queries must not take locks")):
            data = self.listing()
        row = data["tasks"][0]
        self.assertEqual(row.get("resource_name"), "Same Album")
        self.assertEqual((row["resource_type"], row["resource_id"], row["storefront"], row["locale"]),
                         ("album", "1", "us", "en-US"))
        self.assertEqual(row["task_ref"], reference)
        self.assertEqual(row["selection_label"], "all-static")
        self.assertTrue(row["selection"]["all_static"])
        self.assertTrue(row["snapshot_created_at"])
        self.assertTrue(row["selectable"])
        self.assertNotIn(str(self.root), dumps_json(data))
        self.assertNotIn("binding", dumps_json(data))
        self.assertEqual(before, self.tree())

    def test_latest_precedes_status_filters_and_does_not_fall_back_to_completed(self):
        first = self.create()
        second = self.create(new_version=True, bad=True)
        self.assertEqual(first.split("@")[0], second.split("@")[0])
        self.assertEqual(self.listing(latest=True, statuses=["completed"])["tasks"], [])
        rows = self.listing(latest=True, statuses=["failed", "needs_action"])["tasks"]
        self.assertEqual([row["task_ref"] for row in rows], [second])

    def test_query_type_and_status_filters_compose_and_same_names_keep_distinct_ids(self):
        first, second = self.create("123"), self.create("456")
        self.assertEqual(len(self.listing(query="sAmE AL", resource_type="album")["tasks"]), 2)
        self.assertEqual([row["task_ref"] for row in self.listing(query="456", statuses=["completed", "failed"])["tasks"]], [second])
        self.assertEqual([row["task_ref"] for row in self.listing(query=first.split("@")[0])["tasks"]], [first])
        self.assertEqual(self.listing(resource_type="song")["tasks"], [])

    def test_bad_state_shapes_are_isolated_and_broken_latest_remains_latest(self):
        first = self.create()
        second = self.create(new_version=True)
        second_path = self.task_path(second)
        healthy = self.create("2")
        self.rewrite_state(second_path, lambda payload: payload.pop("snapshot"))
        before = self.tree()
        try:
            rows = self.listing(latest=True)["tasks"]
        except (KeyError, TypeError, AttributeError) as error:
            self.fail(f"one damaged payload interrupted healthy task query: {type(error).__name__}")
        self.assertEqual({row["task_ref"] for row in rows}, {second, healthy})
        broken = next(row for row in rows if row["task_ref"] == second)
        self.assertFalse(broken.get("selectable", True))
        self.assertIn("error", broken)
        self.assertEqual(before, self.tree())
        self.assertNotIn(first, {row["task_ref"] for row in rows})

    def test_bad_json_and_hash_do_not_hide_healthy_rows(self):
        bad_json, bad_hash, good = self.create("1"), self.create("2"), self.create("3")
        paths = [self.task_path(ref) / ".apple-artwork-state" / "state.json" for ref in (bad_json, bad_hash)]
        paths[0].write_bytes(b"bad json")
        value = loads_json(paths[1].read_bytes())
        value["payload_sha256"] = "0" * 64
        paths[1].write_text(dumps_json(value), encoding="utf-8")
        rows = self.listing()["tasks"]
        self.assertEqual(sum(bool(row.get("selectable")) for row in rows), 1)
        self.assertEqual(next(row["task_ref"] for row in rows if row.get("selectable")), good)
        self.assertEqual(sum("error" in row for row in rows), 2)

    def test_unrelated_allocation_corruption_does_not_hide_healthy_show(self):
        broken, healthy = self.create("1"), self.create("2")
        broken_path = self.task_path(broken)
        (broken_path / ".apple-artwork-state" / "allocation.json").write_bytes(b"damaged")
        before = self.tree()
        result, code = dispatch(Namespace(command="tasks.show", task_ref=healthy, library=str(self.library)),
                                http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 0, result)
        self.assertEqual(result["data"]["task_id"], healthy.split("@")[0])
        listed = self.listing()
        self.assertEqual([row["task_ref"] for row in listed["tasks"]], [healthy])
        self.assertEqual(len(listed["errors"]), 1)
        self.assertEqual(before, self.tree())

    def test_unknown_dates_sort_last_and_dates_sort_descending(self):
        refs = [self.create(str(number)) for number in (1, 2, 3)]
        for ref, date in zip(refs, (None, "2024-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00")):
            self.rewrite_state(self.task_path(ref), lambda payload, date=date: payload["snapshot"].update(created_at=date))
        rows = self.listing()["tasks"]
        self.assertEqual([row["task_ref"] for row in rows], [refs[2], refs[1], refs[0]])
        self.assertIsNone(rows[-1]["snapshot_created_at"])

    def test_bound_completed_resume_uses_real_execution_without_network(self):
        ref = self.create()
        data, bindings = self.query()
        result, code = dispatch(Namespace(command="resume", task_ref=ref, library=str(self.library),
                                         _selection_binding=bindings[ref], json=True, no_input=True),
                                http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 0, result)
        self.assertEqual(result["run"]["counts"]["reused"], 1)
        self.assertEqual(result["control"]["resume"]["task_ref"], ref)

    def test_changed_state_after_selection_is_stale_without_writes(self):
        ref = self.create()
        _, bindings = self.query()
        self.rewrite_state(self.task_path(ref), lambda payload: payload["control"].update(lifecycle="deferred"))
        before = self.tree()
        result, code = dispatch(Namespace(command="resume", task_ref=ref, library=str(self.library),
                                         _selection_binding=bindings[ref], json=True, no_input=True),
                                http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 2, result)
        self.assertEqual(result["errors"][0]["code"], "PLAN_STALE")
        self.assertEqual(before, self.tree())

    def test_recovery_state_swap_at_lock_barrier_is_rejected_before_repair(self):
        # The unbound v0.4 path validated identity before entering RunState,
        # but its __enter__ repaired a newly appeared transaction first.
        ref = self.create()
        path = self.task_path(ref)
        other = self.root / "other"
        other.mkdir()
        class Interrupted(BaseException):
            pass
        with OutputDirectory(other) as output, RunState(output) as state:
            state.data = {"task_identity": {"other": True}, "task_fingerprint": fingerprint({"other": True})}
            with patch.object(RunState, "_remove_marker", side_effect=Interrupted):
                with self.assertRaises(Interrupted):
                    state.save()
        # Place a pending transaction for the chosen identity to reach the
        # workflow recovery path. Replace it with another identity at flock.
        metadata = path / ".apple-artwork-state"
        with OutputDirectory(path, create=False) as output, RunState(output) as state:
            state.data["control"]["lifecycle"] = "deferred"
            with patch.object(RunState, "_remove_marker", side_effect=Interrupted):
                with self.assertRaises(Interrupted):
                    state.save()
        data, bindings = self.query()
        self.assertTrue(data["tasks"][0].get("recovery_required"), "registered pending state must be selectable without repair")
        self.assertTrue(data["tasks"][0]["selectable"])
        import fcntl
        real_flock = fcntl.flock
        swapped = []
        def barrier(fd, operation):
            if not swapped and operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
                for item in metadata.iterdir():
                    if item.is_file() and item.name != "lock":
                        item.unlink()
                for item in (other / ".apple-artwork-state").iterdir():
                    if item.is_file() and item.name != "lock":
                        (metadata / item.name).write_bytes(item.read_bytes())
                swapped.append(self.tree())
            return real_flock(fd, operation)
        with patch("fcntl.flock", side_effect=barrier):
            result, code = dispatch(Namespace(command="resume", task_ref=ref, library=str(self.library),
                                             _selection_binding=bindings[ref], json=True, no_input=True),
                                    http=HttpClient(NoNetwork()), environ={})
        self.assertTrue(swapped, "controlled lock barrier was not reached")
        self.assertEqual(code, 2, result)
        self.assertEqual(result["errors"][0]["code"], "PLAN_STALE")
        self.assertEqual(swapped[0], self.tree(), "recovery wrote before rejecting the replacement state")

    def test_registered_pending_state_is_readonly_selectable_and_recovers_via_bound_resume(self):
        ref = self.create()
        path = self.task_path(ref)
        class Interrupted(BaseException):
            pass
        with OutputDirectory(path, create=False) as output, RunState(output) as state:
            state.data["control"]["lifecycle"] = "deferred"
            with patch.object(RunState, "_remove_marker", side_effect=Interrupted):
                with self.assertRaises(Interrupted):
                    state.save()
        before = self.tree()
        with patch("fcntl.flock", side_effect=AssertionError("query attempted a lock")):
            data, bindings = self.query()
        self.assertTrue(data["tasks"][0].get("recovery_required"))
        self.assertTrue(data["tasks"][0]["selectable"])
        self.assertEqual(before, self.tree())
        result, code = dispatch(Namespace(command="resume", task_ref=ref, library=str(self.library),
                                         _selection_binding=bindings[ref], json=True, no_input=True),
                                http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 0, result)
        self.assertEqual(result["run"]["counts"]["reused"], 1)
        self.assertFalse((path / ".apple-artwork-state" / "state-transition.json").exists())

    def test_self_consistent_state_of_another_task_is_not_selectable(self):
        wrong, healthy = self.create("1"), self.create("2")
        wrong_path, healthy_path = self.task_path(wrong), self.task_path(healthy)
        metadata = wrong_path / ".apple-artwork-state"
        for name in ("state.json", "state-previous.json"):
            (metadata / name).write_bytes((healthy_path / ".apple-artwork-state" / name).read_bytes())
        rows = self.listing()["tasks"]
        row = next(item for item in rows if item["task_ref"] == wrong)
        self.assertFalse(row["selectable"])
        self.assertEqual(row["resource_id"], "1")
        self.assertIsNone(row["resource_name"])
        self.assertIn("error", row)
        self.assertTrue(next(item for item in rows if item["task_ref"] == healthy)["selectable"])

    def test_incomplete_hash_valid_payloads_remain_individually_unselectable(self):
        damaged = []
        changes = [lambda payload: payload.pop("manifest"),
                   lambda payload: payload.update(manifest_pending={"unknown": True}),
                   lambda payload: payload["snapshot"].pop("manifest"),
                   lambda payload: payload["files"].update({next(iter(payload["files"])): {"status": "committed"}})]
        for number, change in enumerate(changes, 10):
            ref = self.create(str(number))
            damaged.append((self.task_path(ref), change))
        healthy = self.create("20")
        for path, change in damaged:
            self.rewrite_state(path, change)
        before = self.tree()
        rows = self.listing()["tasks"]
        self.assertEqual([row["task_ref"] for row in rows if row["selectable"]], [healthy])
        self.assertEqual(sum("error" in row for row in rows), 4)
        self.assertEqual(before, self.tree())

    def test_lock_replaced_with_nonregular_file_before_open_is_stale(self):
        ref = self.create()
        _, bindings = self.query()
        lock = self.task_path(ref) / ".apple-artwork-state" / "lock"
        original = os.open
        swapped = []
        def barrier(name, flags, *args, **kwargs):
            if name == "lock" and flags & os.O_RDWR and not swapped:
                lock.rename(self.root / "retained-lock")
                os.mkfifo(lock)
                swapped.append(self.tree())
            return original(name, flags, *args, **kwargs)
        with patch("apple_music_artwork.run_state.os.open", new=barrier), patch("os.supports_dir_fd", os.supports_dir_fd | {barrier}):
            result, code = dispatch(Namespace(command="resume", task_ref=ref, library=str(self.library),
                                             _selection_binding=bindings[ref], json=True, no_input=True),
                                    http=HttpClient(NoNetwork()), environ={})
        self.assertTrue(swapped)
        self.assertEqual(code, 2, result)
        self.assertEqual(result["errors"][0]["code"], "PLAN_STALE")
        self.assertEqual(swapped[0], self.tree())

    def test_metadata_replacement_between_admission_and_open_is_stale(self):
        from apple_music_artwork.task_queries import TaskBinding
        ref = self.create()
        path = self.task_path(ref)
        class Interrupted(BaseException):
            pass
        with OutputDirectory(path, create=False) as output, RunState(output) as state:
            state.data["control"]["lifecycle"] = "deferred"
            with patch.object(RunState, "_remove_marker", side_effect=Interrupted):
                with self.assertRaises(Interrupted):
                    state.save()
        _, bindings = self.query()
        metadata_path = path / ".apple-artwork-state"
        original, swapped = TaskBinding.admit, []
        def barrier(binding, output, metadata=None, lock_fd=None):
            original(binding, output, metadata, lock_fd)
            if metadata is None and not swapped:
                metadata_path.rename(self.root / "retained-metadata")
                metadata_path.write_bytes(b"replacement file")
                swapped.append(self.tree(self.root))
        with patch.object(TaskBinding, "admit", new=barrier):
            result, code = dispatch(Namespace(command="resume", task_ref=ref, library=str(self.library),
                                             _selection_binding=bindings[ref], json=True, no_input=True),
                                    http=HttpClient(NoNetwork()), environ={})
        self.assertTrue(swapped)
        self.assertEqual(code, 2, result)
        self.assertEqual(result["errors"][0]["code"], "PLAN_STALE")
        self.assertEqual(swapped[0], self.tree(self.root))

    def test_corrupt_newest_registry_does_not_make_old_version_latest(self):
        old = self.create()
        new = self.create(new_version=True)
        task_id = new.split("@")[0]
        registry = self.library / ".apple-artwork-library" / "tasks" / task_id
        (registry / "v2.json").write_bytes(b"damaged")
        result = self.listing(latest=True)
        self.assertEqual(result["tasks"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["version"], 2)

    def test_missing_navigation_tree_keeps_local_tasks_readable_without_repair(self):
        ref = self.create()
        registry = self.library / ".apple-artwork-library"
        registry.rename(self.root / "retained-navigation")
        before = self.tree()
        self.assertEqual(self.listing()["tasks"][0]["task_ref"], ref)
        self.assertEqual(before, self.tree())

    def test_corrupt_navigation_tree_keeps_independent_local_rows(self):
        ref = self.create()
        registry = self.library / ".apple-artwork-library"
        registry.rename(self.root / "retained-navigation")
        registry.write_bytes(b"not a directory")
        before = self.tree()
        result = self.listing()
        self.assertEqual(result["tasks"][0]["task_ref"], ref)
        self.assertTrue(result["errors"])
        self.assertEqual(before, self.tree())

    def test_selection_bindings_are_immutable_and_not_derived_from_mutable_rows(self):
        ref = self.create()
        data, bindings = self.query()
        binding = bindings[ref]
        with self.assertRaises((AttributeError, TypeError)):
            binding.path = str(self.root / "different")
        data["tasks"][0].update(task_ref="0" * 64 + "@1", resource_name="Changed display")
        result, code = dispatch(Namespace(command="resume", task_ref=ref, library=str(self.library),
                                         _selection_binding=binding, json=True, no_input=True),
                                http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 0, result)
        self.assertEqual(result["control"]["resume"]["task_ref"], ref)

    def test_selected_directory_missing_metadata_is_rejected_without_recreating_it(self):
        ref = self.create()
        _, bindings = self.query()
        path = self.task_path(ref)
        path.rename(self.root / "retained-task")
        path.mkdir()
        (path / "user.txt").write_bytes(b"preserve this replacement")
        before = self.tree()
        result, code = dispatch(Namespace(command="resume", task_ref=ref, library=str(self.library),
                                         _selection_binding=bindings[ref], json=True, no_input=True),
                                http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 2, result)
        self.assertEqual(result["errors"][0]["code"], "PLAN_STALE")
        self.assertEqual(before, self.tree())

    def test_selected_missing_lock_is_rejected_without_creating_a_lock(self):
        ref = self.create()
        _, bindings = self.query()
        lock = self.task_path(ref) / ".apple-artwork-state" / "lock"
        lock.rename(self.root / "retained-lock")
        before = self.tree()
        result, code = dispatch(Namespace(command="resume", task_ref=ref, library=str(self.library),
                                         _selection_binding=bindings[ref], json=True, no_input=True),
                                http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 2, result)
        self.assertEqual(result["errors"][0]["code"], "PLAN_STALE")
        self.assertEqual(before, self.tree())

    def test_show_rechecks_local_allocation_after_location_was_selected(self):
        from apple_music_artwork import workflow
        first = self.create()
        second = self.create(new_version=True)
        first_path, second_path = self.task_path(first), self.task_path(second)
        original = workflow._location
        after_swap = []
        def barrier(args, **kwargs):
            location = original(args, **kwargs)
            (first_path / ".apple-artwork-state" / "allocation.json").write_bytes(
                (second_path / ".apple-artwork-state" / "allocation.json").read_bytes())
            after_swap.append(self.tree())
            return location
        with patch.object(workflow, "_location", side_effect=barrier):
            result, code = dispatch(Namespace(command="tasks.show", task_ref=first, library=str(self.library)),
                                    http=HttpClient(NoNetwork()), environ={})
        self.assertNotEqual(code, 0, result)
        self.assertEqual(after_swap[0], self.tree())

    def test_unbound_resume_retains_existing_missing_lock_recovery_behavior(self):
        ref = self.create()
        path = self.task_path(ref)
        lock = path / ".apple-artwork-state" / "lock"
        lock.rename(self.root / "retained-lock")
        result, code = dispatch(Namespace(command="resume", task_dir=str(path), json=True, no_input=True),
                                http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 0, result)
        self.assertEqual(result["run"]["counts"]["reused"], 1)
        self.assertTrue(lock.is_file())

    def test_execution_lock_barrier_rejects_every_selected_identity_replacement(self):
        import fcntl
        import shutil
        real_flock = fcntl.flock
        for kind in ("state", "allocation", "registry", "root", "task", "metadata", "lock"):
            with self.subTest(kind=kind):
                # Give each scenario its own actual library task; successful
                # preflight must reach the exclusive execution lock barrier.
                self.library = self.root / kind
                ref = self.create()
                path = self.task_path(ref)
                _, bindings = self.query()
                metadata = path / ".apple-artwork-state"
                swapped = []
                def barrier(fd, operation):
                    if not swapped and operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
                        if kind == "state":
                            self.rewrite_state(path, lambda payload: payload["control"].update(lifecycle="deferred"))
                        elif kind in {"allocation", "registry"}:
                            record_path = (metadata / "allocation.json" if kind == "allocation" else
                                self.library / ".apple-artwork-library" / "tasks" / ref.split("@")[0] / "v1.json")
                            value = loads_json(record_path.read_bytes())
                            value["created_at"] = "2000-01-01T00:00:00+00:00"
                            record_path.write_text(dumps_json(value), encoding="utf-8")
                        else:
                            target = {"root": self.library, "task": path, "metadata": metadata, "lock": metadata / "lock"}[kind]
                            retained = self.root / ("retained-" + kind)
                            target.rename(retained)
                            if kind == "lock":
                                target.write_bytes(b"")
                            else:
                                shutil.copytree(retained, target)
                        swapped.append(self.tree(self.root))
                    return real_flock(fd, operation)
                with patch("fcntl.flock", side_effect=barrier):
                    result, code = dispatch(Namespace(command="resume", task_ref=ref, library=str(self.library),
                                                     _selection_binding=bindings[ref], json=True, no_input=True),
                                            http=HttpClient(NoNetwork()), environ={})
                self.assertTrue(swapped, "exclusive execution lock barrier was not reached")
                self.assertEqual(code, 2, result)
                self.assertEqual(result["errors"][0]["code"], "PLAN_STALE")
                self.assertEqual(swapped[0], self.tree(self.root), "stale admission changed retained or replacement files")

    def test_saved_plan_rejects_new_pending_transaction_before_recovery(self):
        # Saved plans also reach the actual RunState lock. Their preflight
        # observations must be admitted before any newly appeared recovery.
        ref = self.create()
        path = self.task_path(ref)
        planned, code = dispatch(Namespace(command="plan", task_dir=str(path)), http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 0, planned)
        plan_path = self.root / "plan.json"
        plan_path.write_text(dumps_json(planned), encoding="utf-8")
        class Interrupted(BaseException):
            pass
        with OutputDirectory(path, create=False) as output, RunState(output) as state:
            state.data["control"]["lifecycle"] = "deferred"
            with patch.object(RunState, "_remove_marker", side_effect=Interrupted):
                with self.assertRaises(Interrupted):
                    state.save()
        before = self.tree()
        result, code = dispatch(Namespace(command="download", plan_file=str(plan_path), json=True, no_input=True),
                                http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 2, result)
        self.assertEqual(result["errors"][0]["code"], "PLAN_STALE")
        self.assertEqual(before, self.tree(), "a stale saved plan repaired a new pending transaction")

    def test_saved_plan_post_lock_check_rejects_transaction_inserted_at_barrier(self):
        import fcntl
        ref = self.create()
        path = self.task_path(ref)
        planned, code = dispatch(Namespace(command="plan", task_dir=str(path)), http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 0, planned)
        plan_file = self.root / "plan.json"
        plan_file.write_text(dumps_json(planned), encoding="utf-8")
        real_flock, entered, after = fcntl.flock, [], []
        class Interrupted(BaseException):
            pass
        def barrier(fd, operation):
            if not entered and operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
                entered.append(True)
                with OutputDirectory(path, create=False) as output, RunState(output) as state:
                    state.data["control"]["lifecycle"] = "deferred"
                    with patch.object(RunState, "_remove_marker", side_effect=Interrupted):
                        with self.assertRaises(Interrupted):
                            state.save()
                after.append(self.tree())
            return real_flock(fd, operation)
        with patch("fcntl.flock", side_effect=barrier):
            result, code = dispatch(Namespace(command="download", plan_file=str(plan_file), json=True, no_input=True),
                                    http=HttpClient(NoNetwork()), environ={})
        self.assertTrue(entered)
        self.assertEqual(code, 2, result)
        self.assertEqual(result["errors"][0]["code"], "PLAN_STALE")
        self.assertEqual(after[0], self.tree())

    def test_saved_plan_does_not_recreate_removed_existing_directory(self):
        ref = self.create()
        path = self.task_path(ref)
        planned, code = dispatch(Namespace(command="plan", task_dir=str(path)), http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 0, planned)
        plan_file = self.root / "plan.json"
        plan_file.write_text(dumps_json(planned), encoding="utf-8")
        path.rename(self.root / "retained-task")
        before = self.tree(self.root)
        result, code = dispatch(Namespace(command="download", plan_file=str(plan_file), json=True, no_input=True),
                                http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 2, result)
        self.assertEqual(result["errors"][0]["code"], "PLAN_STALE")
        self.assertEqual(before, self.tree(self.root))

    def test_fresh_existing_plan_preserves_unbound_missing_lock_policy(self):
        ref = self.create()
        path = self.task_path(ref)
        planned, code = dispatch(Namespace(command="plan", task_dir=str(path)), http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 0, planned)
        plan_file = self.root / "plan.json"
        plan_file.write_text(dumps_json(planned), encoding="utf-8")
        lock = path / ".apple-artwork-state" / "lock"
        lock.rename(self.root / "retained-lock")
        result, code = dispatch(Namespace(command="download", plan_file=str(plan_file), json=True, no_input=True),
                                http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 0, result)
        self.assertEqual(result["run"]["counts"]["reused"], 1)
        self.assertTrue(lock.is_file())

    def test_flat_v1_query_and_plan_are_readonly_then_execution_preserves_upgrade_backup(self):
        from apple_music_artwork.execution import execute
        from apple_music_artwork.workflow import _args
        source = self.root / "source.json"
        source.write_text(dumps_json(response()), encoding="utf-8")
        path = self.root / "flat"
        result, code = execute(_args(Namespace(url="https://music.apple.com/us/album/example/1?l=en-US",
                                               response=str(source), output=str(path), all_static=True)),
                               http=HttpClient(Images()), environ={})
        self.assertEqual(code, 0, result)
        state_file = path / ".apple-artwork-state" / "state.json"
        value = loads_json(state_file.read_bytes())
        value["state_version"] = 1
        state_file.write_text(dumps_json(value), encoding="utf-8")
        original, before = state_file.read_bytes(), self.tree(path)
        shown, code = dispatch(Namespace(command="tasks.show", task_dir=str(path)), http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["data"]["state_version"], 1)
        planned, code = dispatch(Namespace(command="plan", task_dir=str(path)), http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 0, planned)
        self.assertEqual(before, self.tree(path))
        plan_file = self.root / "plan.json"
        plan_file.write_text(dumps_json(planned), encoding="utf-8")
        result, code = dispatch(Namespace(command="download", plan_file=str(plan_file), json=True, no_input=True),
                                http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 0, result)
        self.assertEqual(result["run"]["counts"]["reused"], 1)
        self.assertEqual((path / ".apple-artwork-state" / "state-v1-original.json").read_bytes(), original)
        self.assertEqual(loads_json(state_file.read_bytes())["state_version"], 2)

    def test_resume_with_saved_plan_does_not_recover_a_new_transaction_first(self):
        ref = self.create()
        path = self.task_path(ref)
        planned, code = dispatch(Namespace(command="plan", task_dir=str(path)), http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 0, planned)
        plan_file = self.root / "plan.json"
        plan_file.write_text(dumps_json(planned), encoding="utf-8")
        class Interrupted(BaseException):
            pass
        with OutputDirectory(path, create=False) as output, RunState(output) as state:
            state.data["control"]["lifecycle"] = "deferred"
            with patch.object(RunState, "_remove_marker", side_effect=Interrupted):
                with self.assertRaises(Interrupted):
                    state.save()
        before = self.tree()
        result, code = dispatch(Namespace(command="resume", task_dir=str(path), plan_file=str(plan_file), json=True, no_input=True),
                                http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 2, result)
        self.assertEqual(result["errors"][0]["code"], "PLAN_STALE")
        self.assertEqual(before, self.tree())

    def test_saved_plan_metadata_removed_after_precheck_is_not_recreated(self):
        ref = self.create()
        path = self.task_path(ref)
        planned, code = dispatch(Namespace(command="plan", task_dir=str(path)), http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(code, 0, planned)
        plan_file = self.root / "plan.json"
        plan_file.write_text(dumps_json(planned), encoding="utf-8")
        original_init, moved = RunState.__init__, []
        def initialize(state, output, **kwargs):
            original_init(state, output, **kwargs)
            if state.admission is not None and not state.require_existing_lock:
                admitted = state.admission
                def barrier(output, metadata=None, lock_fd=None):
                    admitted(output, metadata, lock_fd)
                    if metadata is None and not moved:
                        (path / ".apple-artwork-state").rename(self.root / "retained-metadata")
                        moved.append(self.tree(self.root))
                state.admission = barrier
        with patch.object(RunState, "__init__", new=initialize):
            result, code = dispatch(Namespace(command="download", plan_file=str(plan_file), json=True, no_input=True),
                                    http=HttpClient(NoNetwork()), environ={})
        self.assertTrue(moved, "real pre-admission barrier was not reached")
        self.assertEqual(code, 2, result)
        self.assertEqual(result["errors"][0]["code"], "PLAN_STALE")
        self.assertEqual(moved[0], self.tree(self.root), "stale saved plan recreated metadata before rejecting it")

    def _interrupt_pending_state(self, path, phase):
        class Interrupted(BaseException):
            pass
        with OutputDirectory(path, create=False) as output, RunState(output) as state:
            state.data["control"]["lifecycle"] = "deferred"
            if phase == "before_previous":
                operation = patch.object(RunState, "_recover_state_transition", side_effect=Interrupted)
            elif phase == "before_publication":
                original_replace = os.replace
                def stop_publication(source, destination, **kwargs):
                    if destination == "state.json":
                        raise Interrupted()
                    return original_replace(source, destination, **kwargs)
                operation = patch("apple_music_artwork.run_state.os.replace", side_effect=stop_publication)
            else:
                self.assertEqual(phase, "after_publication")
                operation = patch.object(RunState, "_remove_marker", side_effect=Interrupted)
            with operation:
                with self.assertRaises(Interrupted):
                    state.save()
        self.assertTrue((path / ".apple-artwork-state" / "state-transition.json").exists())

    def _damaged_published_previous_is_not_selectable(self, damage):
        broken, healthy = self.create("1"), self.create("2")
        path = self.task_path(broken)
        self._interrupt_pending_state(path, "after_publication")
        previous = path / ".apple-artwork-state" / "state-previous.json"
        if damage == "missing":
            previous.rename(self.root / "retained-previous.json")
        else:
            self.assertEqual(damage, "mismatch")
            # A valid envelope of the wrong revision is still the wrong proof.
            previous.write_bytes((path / ".apple-artwork-state" / "state.json").read_bytes())
        before = self.tree(self.root)
        with patch("fcntl.flock", side_effect=AssertionError("query must not lock or recover")):
            data, bindings = self.query()
        row = next(item for item in data["tasks"] if item["task_ref"] == broken)
        self.assertFalse(row["selectable"], "published pending state lacked its required previous proof")
        self.assertEqual(row["error"]["code"], "RUN_STATE_INVALID")
        self.assertNotIn(broken, bindings)
        self.assertTrue(next(item for item in data["tasks"] if item["task_ref"] == healthy)["selectable"])
        self.assertIn(healthy, bindings)
        self.assertEqual(before, self.tree(self.root))

    def test_published_pending_state_with_missing_previous_is_unselectable(self):
        self._damaged_published_previous_is_not_selectable("missing")

    def test_published_pending_state_with_wrong_previous_is_unselectable(self):
        self._damaged_published_previous_is_not_selectable("mismatch")

    def test_legal_pending_phases_remain_readonly_selectable_and_recoverable(self):
        for phase in ("before_previous", "before_publication", "after_publication"):
            with self.subTest(phase=phase):
                self.library = self.root / phase
                ref = self.create()
                path = self.task_path(ref)
                self._interrupt_pending_state(path, phase)
                before = self.tree(self.root)
                with patch("fcntl.flock", side_effect=AssertionError("query must not lock or recover")):
                    data, bindings = self.query()
                self.assertTrue(data["tasks"][0]["selectable"], data)
                self.assertTrue(data["tasks"][0]["recovery_required"])
                self.assertIn(ref, bindings)
                self.assertEqual(before, self.tree(self.root))
                result, code = dispatch(Namespace(command="resume", task_ref=ref, library=str(self.library),
                                                 _selection_binding=bindings[ref], json=True, no_input=True),
                                        http=HttpClient(NoNetwork()), environ={})
                self.assertEqual(code, 0, result)
                self.assertEqual(result["run"]["counts"]["reused"], 1)
                self.assertFalse((path / ".apple-artwork-state" / "state-transition.json").exists())
