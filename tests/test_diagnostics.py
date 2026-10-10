"""Local diagnostics use synthetic events and process-owned temporary trees."""

from copy import deepcopy
from argparse import Namespace
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest import TestCase
from unittest.mock import patch

from apple_music_artwork.download import OutputDirectory
from apple_music_artwork.motion import ProcessResult


TASK = "a" * 64
ITEM = "file_" + "c" * 64


def event(sequence=1):
    return {"schema_version": 1, "sequence": sequence, "run_id": "00000000-0000-0000-0000-000000000001",
            "task_id": TASK, "plan_id": "b" * 64, "kind": "stage", "stage": "download",
            "item_id": ITEM, "attempt": 1}


class _DiagnosticModule:
    def diagnostics(self):
        self.assertIsNotNone(importlib.util.find_spec("apple_music_artwork.diagnostics"))
        return importlib.import_module("apple_music_artwork.diagnostics")


class DiagnosticsTests(_DiagnosticModule, TestCase):
    def test_plan_event_only_reports_bound_identity_and_counts(self):
        diagnostics = self.diagnostics()
        clean = diagnostics.event_record({"kind": "plan", "plan": {"task_id": TASK, "plan_id": "b" * 64,
                                           "items": [{"url": "https://private.invalid"}], "decisions": [],
                                           "destination": "/home/private", "snapshot": {"body": "secret"}}})
        self.assertEqual(clean, {"kind": "plan", "task_id": TASK, "plan_id": "b" * 64,
                                 "item_count": 1, "decision_count": 0})


    def test_event_projection_filters_nested_external_text_and_nonfinite_values(self):
        diagnostics = self.diagnostics()
        source = {**event(), "kind": "snapshot", "url": "https://secret.invalid/", "message": "private",
                  "filename": "/home/private", "raw_exception": "secret", "Authorization": "secret",
                  "counts": {"completed": 1, "failed": 0, "private": "secret", "pending": -1},
                  "stats": {"requests": 2, "received_bytes": 20, "token": "secret"},
                  "items": [{"item_id": ITEM, "kind": "video", "name": "secret",
                             "stage": "waiting_validation", "verified": False, "total_bytes": 20,
                             "received_bytes": 20, "url": "https://secret.invalid/", "attempt": True}],
                  "reason": "server_cooldown", "retry_at": float("nan")}
        original = deepcopy(source)
        clean = diagnostics.event_record(source)
        self.assertEqual(clean["counts"], {"completed": 1, "failed": 0})
        self.assertEqual(clean["items"][0], {"item_id": ITEM, "kind": "video",
                         "stage": "waiting_validation", "verified": False,
                         "total_bytes": 20, "received_bytes": 20})
        self.assertNotIn("retry_at", clean)
        self.assertNotIn("secret", json.dumps(clean))
        self.assertNotIn("private", json.dumps(clean))
        self.assertEqual(source["items"], original["items"])
        self.assertEqual(diagnostics.event_record({"kind": "https://secret.invalid", "stage": "/private"}), {})
        self.assertEqual(diagnostics.event_record({"kind": "stage", "retry_at": 10 ** 400}), {"kind": "stage"})

    def test_doctor_never_uses_network_or_claims_a_real_media_decode(self):
        diagnostics = self.diagnostics()
        calls = []

        class Runner:
            def run(self, argv, **kwargs):
                calls.append((argv, kwargs))
                name = "ffmpeg" if len(calls) == 1 else "ffprobe"
                return ProcessResult(0, f"{name} version 9.0.2 Copyright\n".encode(), b"private diagnostics")

        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No network")):
            result = diagnostics.doctor(environ={"PATH": "", "APPLE_ARTWORK_FFMPEG": sys.executable,
                                                "APPLE_ARTWORK_FFPROBE": sys.executable}, runner=Runner())
        self.assertFalse(result["network_used"])
        self.assertEqual(result["verification"]["media_decode"], "not_run")
        self.assertEqual([argv[1:] for argv, _ in calls], [["-version"], ["-version"]])
        self.assertTrue(result["capabilities"]["static_download"])
        self.assertNotIn(sys.executable, json.dumps(result))
        self.assertNotIn("private", json.dumps(result))
        with patch("apple_music_artwork.diagnostics.shutil.which", return_value=None):
            missing = diagnostics.doctor(environ={"PATH": ""})
        self.assertFalse(missing["capabilities"]["motion_download"])
        self.assertTrue(missing["capabilities"]["static_download"])

    def test_skipped_issue_count_accepts_only_bounded_nonnegative_integers(self):
        diagnostics = self.diagnostics()
        for value in (0, 2, 2 ** 63 - 1):
            with self.subTest(value=value):
                self.assertEqual(diagnostics.event_record({"kind": "snapshot", "counts": {"skipped_issues": value}}),
                                 {"kind": "snapshot", "counts": {"skipped_issues": value}})
        for value in (-1, True, 1.5, "2", None, 2 ** 63):
            with self.subTest(value=value):
                self.assertEqual(diagnostics.event_record({"kind": "snapshot", "counts": {"skipped_issues": value}}),
                                 {"kind": "snapshot", "counts": {}})


class JournalTests(_DiagnosticModule, TestCase):
    def setUp(self):
        self.temporary_scope = tempfile.TemporaryDirectory(prefix="artwork-diagnostics-")
        self.addCleanup(self.temporary_scope.cleanup)
        self.folder = Path(self.temporary_scope.name).resolve()

    def snapshot(self):
        return {str(path.relative_to(self.folder)): (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
                for path in self.folder.rglob("*") if path.is_file() and not path.is_symlink()}

    def test_real_download_summary_preserves_status_lifecycle_counts_and_decision_actions(self):
        from apple_music_artwork.protocol import decision, LIFECYCLES
        diagnostics = self.diagnostics()
        with OutputDirectory(self.folder) as directory:
            journal = diagnostics.EventJournal(directory, task_id=TASK)
            issue = decision("b" * 64, "item_issue", "HTTP_TRANSIENT_ERROR", [ITEM],
                             ["retry", "defer", "derive", "new_version", "skip", "inspect"])
            issue["retry_at"] = 1600
            for lifecycle in LIFECYCLES:
                manifest = {"schema_version": 2, "status": "success",
                            "run": {"counts": {"total": 2, "new_downloaded": 1, "reused": 1,
                                               "semantic_references": 3, "skipped_issues": 2},
                                    "stats": {"requests": 1, "local_reused": 1}},
                            "control": {"protocol_version": 1, "lifecycle": lifecycle,
                                        "task_id": TASK, "plan_id": "b" * 64}}
                journal.summary(result=manifest, unresolved=[issue])
                self.assertEqual(journal.warnings, [])
                saved = journal.read()["summary"]
                self.assertEqual(saved["result"]["status"], "success")
                self.assertEqual(saved["result"]["lifecycle"], lifecycle)
                self.assertEqual(saved["result"]["counts"], manifest["run"]["counts"])
                self.assertEqual(saved["result"]["stats"], manifest["run"]["stats"])
                self.assertEqual(saved["unresolved"][0]["code"], "HTTP_TRANSIENT_ERROR")
                self.assertEqual(saved["unresolved"][0]["item_ids"], [ITEM])
                self.assertEqual(saved["unresolved"][0]["retry_at"], 1600)
                self.assertEqual(saved["unresolved"][0]["allowed_actions"], [option["action"] for option in issue["options"]])

    def test_real_planning_issue_keeps_its_exact_asset_identity_in_diagnostics(self):
        from apple_music_artwork.client import HttpClient
        from apple_music_artwork.planning import preflight
        diagnostics = self.diagnostics()
        source = self.folder / "response.json"
        source.write_text(json.dumps({"data": [{"id": "1", "type": "albums", "attributes": {
            "artwork": {"url": "https://a.mzstatic.com/synthetic/{w}x{h}bb.jpg", "height": 4}}}]}))
        args = Namespace(url="https://music.apple.com/us/album/synthetic/1?l=en-US", response=str(source),
                         output=str(self.folder / "task"), list=False, semantic=None, asset_id=None,
                         all_static=False, all_media=False, all_motion=False, inspect_motion=False,
                         compare_storefronts=None, token_env="APPLE_MUSIC_DEVELOPER_TOKEN", provider="web",
                         width=None, height=None, format=None, motion_profile="best", jobs=2,
                         library=None, new_version=False, no_input=True)
        class NoNetwork:
            def request(self, *_args, **_kwargs):
                raise AssertionError("No network is allowed for this synthetic preflight")
        plan = preflight(args, http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(len(plan["decisions"]), 1)
        issue_id = plan["items"][0]["item_id"]
        self.assertRegex(issue_id, r"^issue_asset_[0-9a-f]{64}$")
        self.assertFalse(Path(args.output).exists())
        with OutputDirectory(self.folder / "diagnostics") as directory:
            journal = diagnostics.EventJournal(directory, task_id=plan["task_id"])
            journal.summary(result={"status": "failed"}, unresolved=plan["decisions"])
            saved = journal.read()["summary"]["unresolved"][0]
            self.assertEqual(saved["item_ids"], [issue_id])
            self.assertEqual(saved["decision_id"], plan["decisions"][0]["decision_id"])
            self.assertEqual(diagnostics.event_record({"kind": "stage", "item_id": "issue_private"}), {"kind": "stage"})

    def test_rotation_is_bounded_and_read_only_queries_leave_every_byte_unchanged(self):
        diagnostics = self.diagnostics()
        with OutputDirectory(self.folder) as directory:
            journal = diagnostics.EventJournal(directory, task_id=TASK, max_file_bytes=700)
            for sequence in range(1, 21):
                journal.emit({**event(sequence), "body": "private"})
            journal.flush()
            journal.summary(result={"status": "failed", "message": "private", "run": {"counts": {"completed": 1}}},
                            unresolved=[{"code": "HTTP_TRANSIENT_ERROR", "reason": "server_cooldown",
                                         "retry_at": 1600, "allowed_actions": ["retry", "defer"], "message": "private"}])
            journal.close()
            self.assertEqual(journal.warnings, [])
            logs = list(self.folder.glob("events-*.jsonl"))
            self.assertEqual(len(logs), 3)
            self.assertTrue(all(path.stat().st_size <= 700 for path in logs))
            before = self.snapshot()
            read_only = diagnostics.EventJournal(directory, task_id=TASK, read_only=True)
            result = read_only.read()
            read_only.close()
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(result["events"][-1]["sequence"], 20)
            self.assertEqual(result["summary"]["result"]["status"], "failed")
            self.assertEqual(result["summary"]["unresolved"][0]["retry_at"], 1600)
            self.assertNotIn("private", json.dumps(result))

    def test_unknown_files_and_replaced_owned_files_are_retained_without_overwrite(self):
        diagnostics = self.diagnostics()
        unknown = self.folder / "events-1.jsonl"
        unknown.write_bytes(b"unregistered\n")
        with OutputDirectory(self.folder) as directory:
            journal = diagnostics.EventJournal(directory, task_id=TASK)
            journal.emit(event())
            journal.flush()
            self.assertTrue(journal.warnings)
            self.assertEqual(unknown.read_bytes(), b"unregistered\n")
            self.assertEqual(set(path.name for path in self.folder.iterdir()), {unknown.name})
        # Use a separate temporary directory to exercise replacement of a
        # registered log without changing the first directory's unregistered entry.
        second = self.folder / "second"
        with OutputDirectory(second) as directory:
            journal = diagnostics.EventJournal(directory, task_id=TASK, max_file_bytes=700)
            journal.emit(event())
            journal.flush()
            log = next(second.glob("events-*.jsonl"))
            replacement = second / "replacement"
            replacement.write_bytes(log.read_bytes())
            os.replace(replacement, log)
            before = log.read_bytes(), log.stat().st_ino
            journal.emit(event(2))
            journal.flush()
            self.assertTrue(journal.warnings)
            self.assertEqual((log.read_bytes(), log.stat().st_ino), before)

    def test_log_failure_is_a_warning_and_does_not_write_result_or_erase_unresolved_summary(self):
        diagnostics = self.diagnostics()
        with OutputDirectory(self.folder) as directory:
            journal = diagnostics.EventJournal(directory, task_id=TASK)
            journal.summary(result={"status": "failed"}, unresolved=[{"code": "NETWORK_ERROR"}])
            before = self.snapshot()
            with patch.object(directory, "allocate_temp", side_effect=OSError("private /home/path")):
                journal.emit(event())
                journal.flush()
            self.assertTrue(journal.warnings)
            self.assertNotIn("private", json.dumps(journal.warnings))
            self.assertEqual(self.snapshot(), before)

    def test_pending_publication_is_read_only_in_query_and_recoverable_on_execution(self):
        diagnostics = self.diagnostics()
        with OutputDirectory(self.folder) as directory:
            journal = diagnostics.EventJournal(directory, task_id=TASK)
            with patch.object(journal, "_recover_pending", side_effect=OSError("synthetic interruption")):
                journal.emit(event())
                journal.flush()
            before = self.snapshot()
            query = diagnostics.EventJournal(directory, task_id=TASK, read_only=True)
            self.assertTrue(query.read()["warnings"])
            self.assertEqual(self.snapshot(), before)
            resumed = diagnostics.EventJournal(directory, task_id=TASK)
            self.assertEqual(resumed.warnings, [])
            self.assertEqual(resumed.read()["events"], [event()])
            self.assertFalse(list(self.folder.glob("*.part")))

    def test_rotation_recovers_before_and_after_every_durable_checkpoint(self):
        diagnostics = self.diagnostics()
        for after in (False, True):
            for checkpoint in range(1, 5):
                with self.subTest(after=after, checkpoint=checkpoint):
                    folder = self.folder / f"rotation-{after}-{checkpoint}"
                    with OutputDirectory(folder) as directory:
                        journal = diagnostics.EventJournal(directory, task_id=TASK, max_file_bytes=700)
                        for sequence in range(1, 4):
                            journal.emit(event(sequence))
                            journal.flush()
                        calls = []
                        original = journal._store_ledger
                        def interrupt():
                            calls.append(True)
                            if len(calls) == checkpoint and not after:
                                raise OSError("synthetic interruption")
                            original()
                            if len(calls) == checkpoint and after:
                                raise OSError("synthetic interruption")
                        with patch.object(journal, "_store_ledger", side_effect=interrupt):
                            journal.emit(event(4))
                            journal.flush()
                        self.assertTrue(journal.warnings)
                        unregistered = {path.name: path.read_bytes() for path in folder.glob("*.part")} if checkpoint == 1 and not after else {}
                        resumed = diagnostics.EventJournal(directory, task_id=TASK, max_file_bytes=700)
                        self.assertEqual(resumed.warnings, [])
                        expected = [2, 3, 4] if checkpoint >= 3 or (after and checkpoint >= 2) else [1, 2, 3]
                        self.assertEqual([entry["sequence"] for entry in resumed.read()["events"]], expected)
                        self.assertEqual(len(list(folder.glob("events-*.jsonl"))), 3)
                        for name, body in unregistered.items():
                            self.assertEqual((folder / name).read_bytes(), body)

    def test_interrupted_cleanup_of_unsealed_candidate_can_finish_on_next_execution(self):
        diagnostics = self.diagnostics()
        with OutputDirectory(self.folder) as directory:
            journal = diagnostics.EventJournal(directory, task_id=TASK)
            original, calls = journal._store_ledger, []
            def interrupt_before_sealing():
                calls.append(True)
                if len(calls) == 2:
                    raise OSError("synthetic interruption")
                original()
            with patch.object(journal, "_store_ledger", side_effect=interrupt_before_sealing):
                journal.emit(event())
                journal.flush()
            with patch.object(diagnostics.EventJournal, "_store_ledger", side_effect=OSError("after cleanup")):
                interrupted = diagnostics.EventJournal(directory, task_id=TASK)
            self.assertTrue(interrupted.warnings)
            resumed = diagnostics.EventJournal(directory, task_id=TASK)
            self.assertEqual(resumed.warnings, [])
            self.assertEqual(resumed.read()["events"], [])
            resumed.emit(event(2))
            resumed.flush()
            self.assertEqual(resumed.read()["events"], [event(2)])

    def test_query_on_empty_directory_creates_nothing_and_mismatched_task_is_rejected(self):
        diagnostics = self.diagnostics()
        with OutputDirectory(self.folder) as directory:
            query = diagnostics.EventJournal(directory, task_id=TASK, read_only=True)
            self.assertEqual(query.read()["events"], [])
            self.assertEqual(list(self.folder.iterdir()), [])
            journal = diagnostics.EventJournal(directory, task_id=TASK)
            journal.emit(event())
            journal.flush()
            before = self.snapshot()
            wrong = diagnostics.EventJournal(directory, task_id="b" * 64)
            wrong.emit(event(2))
            wrong.close()
            self.assertTrue(wrong.warnings)
            self.assertEqual(self.snapshot(), before)
