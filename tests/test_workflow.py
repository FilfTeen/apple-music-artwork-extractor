"""End-to-end offline v0.4 workflow, including bounded user decisions."""

from argparse import Namespace
from contextlib import chdir
from copy import deepcopy
from importlib.util import find_spec
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from apple_music_artwork.client import HttpClient, HttpResponse
from apple_music_artwork.errors import ArtworkError
from apple_music_artwork.jsonio import dumps_json, loads_json
from test_execution import response, Images, NoNetwork
from test_download import image_bytes


class WorkflowTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "response.json"
        self.source.write_text(dumps_json(response(2)), encoding="utf-8")
        self.args = Namespace(command="download", url="https://music.apple.com/us/album/synthetic/1?l=en-US",
                              response=str(self.source), output=str(self.root / "task"), list=False, semantic=None,
                              asset_id=None, all_static=True, all_media=False, all_motion=False, inspect_motion=False,
                              compare_storefronts=None, token_env="APPLE_MUSIC_DEVELOPER_TOKEN", provider="web",
                              width=None, height=None, format=None, motion_profile="best", jobs=2, progress="never",
                              json=True, library=None, new_version=False, no_input=True, plan_file=None,
                              decisions_file=None, task_ref=None, task_dir=None)

    def run_flow(self, transport=None, **kwargs):
        self.assertIsNotNone(find_spec("apple_music_artwork.workflow"), "workflow entry point is missing")
        from apple_music_artwork.workflow import dispatch
        return dispatch(self.args, http=HttpClient(transport or Images()), environ={}, **kwargs)

    def test_layout_and_completed_reuse_keep_schema2_and_zero_network(self):
        result, code = self.run_flow()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["control"]["lifecycle"], "completed")
        self.assertTrue(all(item["path"].startswith("images/") for item in result["downloads"]))
        original = deepcopy(result["downloads"][0]["validation"])
        again, code = self.run_flow(NoNetwork())
        self.assertEqual(code, 0, again)
        self.assertEqual(again["run"]["counts"]["reused"], 2)
        self.assertEqual(again["downloads"][0]["validation"], original)

    def test_independent_success_then_saved_pending_decision(self):
        result, code = self.run_flow(Images(bad={"synthetic-1"}))
        self.assertEqual(code, 8, result)
        self.assertEqual(result["run"]["counts"]["new_downloaded"], 1)
        self.assertEqual(result["control"]["lifecycle"], "needs_action")
        self.assertEqual(result["manifest_report"]["status"], "saved")
        self.assertTrue(result["control"]["decisions"])

    def test_retry_in_same_invocation_uses_bound_choice_and_keeps_history(self):
        self.args.json = self.args.no_input = False
        images = Images(bad={"synthetic-1"})
        contexts = []
        def choose(context):
            contexts.append(context)
            images.bad.clear()
            return {"plan_id": context["plan_id"], "decision_id": context["decision_id"], "action": "retry", "params": {}}
        result, code = self.run_flow(images, decision_provider=choose)
        self.assertEqual(code, 0, result)
        self.assertEqual(len(contexts), 1)
        self.assertEqual(len(images.calls), 3)
        self.assertTrue(any(item["action"] == "retry" for item in result["control"]["history"]))
        self.assertEqual(result["run"]["counts"]["new_downloaded"], 2)

    def test_only_explicit_defer_returns_nine_and_eof_returns_eight(self):
        self.args.json = self.args.no_input = False
        for action, expected in ((None, 8), ("defer", 9)):
            self.args.output = str(self.root / (action or "eof"))
            def choose(context):
                return None if action is None else {"plan_id": context["plan_id"], "decision_id": context["decision_id"], "action": action, "params": {}}
            result, code = self.run_flow(Images(bad={"synthetic-1"}), decision_provider=choose)
            self.assertEqual(code, expected, result)
            self.assertEqual(result["manifest_report"]["status"], "saved")

    def test_manifest_save_failure_never_claims_reliably_pending(self):
        self.assertIsNotNone(find_spec("apple_music_artwork.workflow"), "workflow entry point is missing")
        with patch("apple_music_artwork.run_state.RunState.save_manifest", side_effect=ArtworkError("RUN_STATE_WRITE_FAILED", "injected", "download")):
            result, code = self.run_flow(Images(bad={"synthetic-1"}))
        self.assertEqual(code, 6, result)
        self.assertNotEqual(result["control"]["lifecycle"], "needs_action")

    def test_resume_needs_only_frozen_task_when_original_offline_json_is_moved(self):
        first, code = self.run_flow()
        self.assertEqual(code, 0, first)
        self.source.rename(self.root / "moved-response.json")
        self.args = Namespace(command="resume", task_dir=str(self.root / "task"), task_ref=None,
                              library=None, json=True, no_input=True, jobs=2)
        result, code = self.run_flow(NoNetwork())
        self.assertEqual(code, 0, result)
        self.assertEqual(result["run"]["counts"]["reused"], 2)

    def test_stale_plan_rejects_modified_file_without_downloading(self):
        first, code = self.run_flow()
        self.assertEqual(code, 0, first)
        self.args.command = "plan"
        planned, code = self.run_flow(NoNetwork())
        saved = self.root / "plan.json"
        saved.write_text(dumps_json(planned), encoding="utf-8")
        changed = Path(self.args.output) / first["downloads"][0]["path"]
        changed.write_bytes(b"user replacement")
        self.args.command, self.args.plan_file = "download", str(saved)
        result, code = self.run_flow(NoNetwork())
        self.assertEqual(code, 2, result)
        self.assertEqual(result["errors"][0]["code"], "PLAN_STALE")
        self.assertEqual(changed.read_bytes(), b"user replacement")
        updated = result["control"]["updated_plan"]
        self.assertEqual(updated["items"][0]["action"], "needs_action")
        self.assertTrue(updated["decisions"])

    def test_saved_plan_executes_without_repeating_source_or_selection_arguments(self):
        self.args.command = "plan"
        planned, code = self.run_flow(NoNetwork())
        saved = self.root / "plan.json"
        saved.write_text(dumps_json(planned), encoding="utf-8")
        self.source.rename(self.root / "moved.json")
        self.args = Namespace(command="download", plan_file=str(saved), json=True, no_input=True)
        result, code = self.run_flow()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["run"]["counts"]["new_downloaded"], 2)

    def test_explicit_default_quality_conflict_is_rejected_against_saved_plan(self):
        self.args.command, self.args.motion_profile = "plan", "compatible"
        planned, code = self.run_flow(NoNetwork())
        saved = self.root / "plan.json"
        saved.write_text(dumps_json(planned), encoding="utf-8")
        self.args = Namespace(command="download", plan_file=str(saved), json=True, no_input=True,
                              motion_profile="best", explicit_options=["motion_profile"])
        result, code = self.run_flow(NoNetwork())
        self.assertEqual(code, 2, result)
        self.assertFalse((self.root / "task").exists())

    def test_explicit_defer_show_choice_resume_cycle(self):
        self.args.json = self.args.no_input = False
        def defer(context):
            return {"plan_id": context["plan_id"], "decision_id": context["decision_id"], "action": "defer", "params": {}}
        first, code = self.run_flow(Images(bad={"synthetic-1"}), decision_provider=defer)
        self.assertEqual(code, 9, first)
        self.args = Namespace(command="tasks.show", task_dir=str(self.root / "task"), json=True)
        shown, code = self.run_flow(NoNetwork())
        context = shown["data"]["control"]["decisions"][0]
        choices = self.root / "choices.json"
        choices.write_text(dumps_json({"protocol_version": 1, "choices": [{"plan_id": context["plan_id"],
                           "decision_id": context["decision_id"], "action": "retry", "params": {}}]}), encoding="utf-8")
        self.args = Namespace(command="resume", task_dir=str(self.root / "task"), json=True, no_input=True,
                              decisions_file=str(choices))
        result, code = self.run_flow()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["run"]["counts"]["reused"], 1)
        self.assertTrue(any(item.get("code") == "ASSET_VALIDATION_FAILED" for item in result["control"]["history"]))

    def test_replanned_pending_choice_is_consumed_before_new_attempt(self):
        first, code = self.run_flow(Images(bad={"synthetic-1"}))
        self.assertEqual(code, 8, first)
        self.args.command = "plan"
        planned, code = self.run_flow(NoNetwork())
        context = planned["data"]["decisions"][0]
        saved, choices = self.root / "plan.json", self.root / "choices.json"
        saved.write_text(dumps_json(planned), encoding="utf-8")
        choices.write_text(dumps_json({"protocol_version": 1, "choices": [{"plan_id": context["plan_id"],
                           "decision_id": context["decision_id"], "action": "retry", "params": {}}]}), encoding="utf-8")
        self.args = Namespace(command="download", plan_file=str(saved), decisions_file=str(choices), json=True, no_input=True)
        transport = Images()
        result, code = self.run_flow(transport)
        self.assertEqual(code, 0, result)
        self.assertEqual(len(transport.calls), 1)

    def test_unoffered_machine_choice_is_rejected_without_media(self):
        choices = self.root / "choices.json"
        choices.write_text(dumps_json({"protocol_version": 1, "choices": [{"plan_id": "a" * 64,
                           "decision_id": "b" * 64, "action": "retry", "params": {}}]}), encoding="utf-8")
        self.args.decisions_file = str(choices)
        result, code = self.run_flow(NoNetwork())
        self.assertEqual(code, 2, result)
        self.assertEqual(result["errors"][0]["code"], "DECISION_STALE")

    def test_diagnostics_directory_failure_does_not_prevent_downloads(self):
        self.assertIsNotNone(find_spec("apple_music_artwork.workflow"))
        from apple_music_artwork.download import OutputDirectory
        real_child = OutputDirectory.child
        def child(output, name, **kwargs):
            if name == "diagnostics":
                raise ArtworkError("OUTPUT_WRITE_FAILED", "injected", "download")
            return real_child(output, name, **kwargs)
        with patch.object(OutputDirectory, "child", child):
            result, code = self.run_flow()
        self.assertEqual(code, 0, result)
        self.assertTrue(any(item["code"] == "DIAGNOSTICS_UNAVAILABLE" for item in result["warnings"]))

    def test_actual_resume_recovers_a_pending_state_transaction(self):
        self.assertIsNotNone(find_spec("apple_music_artwork.workflow"))
        from apple_music_artwork.run_state import RunState
        class Crash(BaseException):
            pass
        with patch.object(RunState, "_remove_marker", side_effect=Crash):
            with self.assertRaises(Crash):
                self.run_flow()
        marker = self.root / "task" / ".apple-artwork-state" / "state-transition.json"
        self.assertTrue(marker.exists())
        self.args = Namespace(command="plan", task_dir=str(self.root / "task"), json=True, no_input=True)
        queried, code = self.run_flow(NoNetwork())
        self.assertEqual(code, 6, queried)
        self.assertTrue(marker.exists())
        self.args.command = "resume"
        resumed, code = self.run_flow()
        self.assertEqual(code, 0, resumed)
        self.assertFalse(marker.exists())

    def test_library_reference_preserves_explicit_old_version(self):
        self.args.output, self.args.library = None, str(self.root / "library")
        first, code = self.run_flow()
        self.assertEqual(code, 0, first)
        original_ref = first["control"]["resume"]["task_ref"]
        self.args.new_version = True
        second, code = self.run_flow()
        self.assertEqual(code, 0, second)
        self.args = Namespace(command="resume", task_ref=original_ref, library=str(self.root / "library"), json=True, no_input=True)
        old, code = self.run_flow(NoNetwork())
        self.assertEqual(code, 0, old)
        self.assertEqual(old["control"]["resume"]["task_ref"], original_ref)

    def test_a_new_directory_created_after_planning_invalidates_the_plan(self):
        self.args.command = "plan"
        planned, code = self.run_flow(NoNetwork())
        saved = self.root / "plan.json"
        saved.write_text(dumps_json(planned), encoding="utf-8")
        output = self.root / "task"
        output.mkdir()
        (output / "user.txt").write_bytes(b"user-owned")
        self.args = Namespace(command="download", plan_file=str(saved), json=True, no_input=True)
        result, code = self.run_flow(NoNetwork())
        self.assertEqual(code, 2, result)
        self.assertEqual(result["errors"][0]["code"], "PLAN_STALE")
        self.assertEqual((output / "user.txt").read_bytes(), b"user-owned")
        self.assertFalse((output / ".apple-artwork-state").exists())

    def test_legacy_manifest_plan_and_execution_use_identical_frozen_requests(self):
        from apple_music_artwork.execution import execute
        self.args.response = None
        with patch("apple_music_artwork.execution.AppleMusicClient.fetch", return_value=response()):
            first, code = execute(self.args, http=HttpClient(Images()), environ={})
        self.assertEqual(code, 0, first)
        legacy = self.root / "legacy"
        legacy.mkdir()
        old = loads_json((self.root / "task" / "manifest.json").read_bytes())
        old.pop("run")
        for item in old["downloads"]:
            item.pop("action")
            (legacy / item["path"]).write_bytes((self.root / "task" / item["path"]).read_bytes())
        (legacy / "manifest.json").write_text(dumps_json(old), encoding="utf-8")
        self.args.output, self.args.command = str(legacy), "plan"
        with patch("apple_music_artwork.execution.AppleMusicClient.fetch", side_effect=AssertionError("legacy preflight cannot refresh catalog")):
            planned, code = self.run_flow(NoNetwork())
        self.assertEqual(code, 0, planned)
        self.assertEqual(planned["data"]["items"][0]["action"], "validate_local")
        self.assertFalse((legacy / ".apple-artwork-state").exists())
        saved = self.root / "legacy-plan.json"
        saved.write_text(dumps_json(planned), encoding="utf-8")
        self.args = Namespace(command="download", plan_file=str(saved), json=True, no_input=True)
        result, code = self.run_flow(NoNetwork())
        self.assertEqual(code, 0, result)
        self.assertEqual(result["downloads"][0]["url"], planned["data"]["snapshot"]["files"][0]["request"]["url"])
        self.assertTrue(result["downloads"][0]["local_verification"]["decoded_this_run"])

    def test_explicit_smaller_rendition_creates_related_task_and_preserves_parent(self):
        self.args.output, self.args.library = None, str(self.root / "library")
        self.args.json = self.args.no_input = False
        original_args = deepcopy(self.args)
        class SizeTransport:
            def __init__(self):
                self.calls = []
            def request(self, url, **kwargs):
                self.calls.append(url)
                size = (2, 2) if "/2x2" in url else (4, 4)
                body = b"broken" if size == (4, 4) and "synthetic-1" in url else image_bytes(size)
                return HttpResponse(200, {"Content-Type": "image/jpeg"}, body)
        def choose(context):
            option = next(option for option in context["options"] if option["action"] == "derive" and option["params"].get("width") == 2)
            return {"plan_id": context["plan_id"], "decision_id": context["decision_id"],
                    "action": option["action"], "params": option["params"]}
        transport = SizeTransport()
        child, code = self.run_flow(transport, decision_provider=choose)
        self.assertEqual(code, 0, child)
        self.assertEqual(child["options"]["width"], 2)
        self.assertEqual(child["run"]["counts"]["new_downloaded"], 2)
        self.assertEqual(len(transport.calls), 4)
        parent_id = child["control"]["parent"]["task_id"]
        self.assertNotEqual(parent_id, child["control"]["task_id"])
        from apple_music_artwork import library
        parent = library.resolve(self.root / "library", parent_id)
        parent_result = loads_json((self.root / "library" / parent["relative_path"] / "manifest.json").read_bytes())
        self.assertIsNone(parent_result["options"]["width"])
        self.assertEqual(parent_result["control"]["lifecycle"], "deferred")
        successful = next(item for item in parent_result["downloads"] if item["status"] == "downloaded")
        proof = (self.root / "library" / parent["relative_path"] / successful["path"]).read_bytes()
        self.args = original_args
        self.args.json = self.args.no_input = True
        pending, code = self.run_flow(NoNetwork())
        self.assertEqual(code, 8, pending)
        self.assertEqual(len(library.list_tasks(self.root / "library")), 2)
        self.assertEqual((self.root / "library" / parent["relative_path"] / successful["path"]).read_bytes(), proof)

    def test_child_start_failure_keeps_saved_parent_and_resume_in_final_result(self):
        from apple_music_artwork import workflow
        self.args.json = self.args.no_input = False
        real_preflight = workflow.preflight
        def choose(context):
            option = next(option for option in context["options"] if option["action"] == "derive")
            return {"plan_id": context["plan_id"], "decision_id": context["decision_id"],
                    "action": option["action"], "params": option["params"]}
        error = ArtworkError("OUTPUT_BUSY", "Synthetic child start failure.", "download")
        def child_preflight(args, **kwargs):
            if getattr(args, "_parent_record", None):
                raise error
            return real_preflight(args, **kwargs)
        for phase in ("preflight", "allocation"):
            self.args.output = str(self.root / phase)
            target = "apple_music_artwork.workflow." + ("preflight" if phase == "preflight" else "library.allocate")
            effect = child_preflight if phase == "preflight" else error
            with self.subTest(phase=phase), chdir(self.root), patch(target, side_effect=effect):
                result, code = self.run_flow(Images(bad={"synthetic-1"}), decision_provider=choose)
            self.assertEqual(code, 6, result)
            saved = loads_json((Path(self.args.output) / "manifest.json").read_bytes())
            self.assertEqual(result["control"]["task_id"], saved["control"]["task_id"])
            self.assertEqual(result["control"]["resume"], saved["control"]["resume"])
            self.assertEqual(result["manifest_report"]["status"], "saved")
            self.assertEqual(result["control"]["derived_task_start"]["status"], "failed")
            self.assertEqual(result["errors"][-1]["code"], "OUTPUT_BUSY")
            self.assertEqual(len(result["downloads"]), 2)

    def test_derived_child_binds_parent_version_and_repeating_choice_reuses_that_child(self):
        self.args.output, self.args.library, self.args.response = None, str(self.root / "library"), None
        self.args.json = self.args.no_input = False
        second_source = response(2)
        attributes = second_source["data"][0]["attributes"]
        for artwork in (attributes["artwork"], attributes["editorialArtwork"]["staticDetailSquare"]):
            artwork["url"] = artwork["url"].replace("/synthetic-", "/revision-two/synthetic-")
        class SizeTransport:
            def __init__(self):
                self.calls = []
            def request(self, url, **kwargs):
                self.calls.append(url)
                size = (2, 2) if "/2x2" in url else (4, 4)
                body = b"broken" if size == (4, 4) and "synthetic-1" in url else image_bytes(size)
                return HttpResponse(200, {"Content-Type": "image/jpeg"}, body)
        def choose(context):
            option = next(option for option in context["options"] if option["action"] == "derive" and option["params"].get("width") == 2)
            return {"plan_id": context["plan_id"], "decision_id": context["decision_id"],
                    "action": option["action"], "params": option["params"]}
        transport = SizeTransport()
        with patch("apple_music_artwork.execution.AppleMusicClient.fetch", side_effect=[response(2), second_source]):
            first, code = self.run_flow(transport, decision_provider=choose)
            self.assertEqual(code, 0, first)
            self.args.new_version = True
            second, code = self.run_flow(transport, decision_provider=choose)
        self.assertEqual(code, 0, second)
        self.assertEqual(second["control"]["parent"]["version"], 2)
        self.assertEqual(second["control"]["task_id"], first["control"]["task_id"])
        self.assertTrue(second["control"]["resume"]["task_ref"].endswith("@2"))
        self.assertTrue(all("/revision-two/" in item["url"] for item in second["downloads"]))
        self.assertEqual(second["run"]["counts"]["new_downloaded"], 2)
        self.assertEqual(len(transport.calls), 8)
        parent_id = second["control"]["parent"]["task_id"]
        self.args = Namespace(command="resume", task_ref=parent_id + "@2", library=str(self.root / "library"),
                              json=False, no_input=False)
        repeated, code = self.run_flow(NoNetwork(), decision_provider=choose)
        self.assertEqual(code, 0, repeated)
        self.assertEqual(repeated["control"]["resume"], second["control"]["resume"])
        self.assertEqual(repeated["run"]["counts"]["reused"], 2)
        self.args.task_ref = parent_id + "@1"
        older, code = self.run_flow(NoNetwork(), decision_provider=choose)
        self.assertEqual(code, 0, older)
        self.assertEqual(older["control"]["resume"], first["control"]["resume"])
        self.assertEqual(older["run"]["counts"]["reused"], 2)
        from apple_music_artwork import library
        children = [item for item in library.list_tasks(self.root / "library") if item["task_id"] == second["control"]["task_id"]]
        self.assertEqual([item["version"] for item in children], [1, 2])
        self.assertEqual(children[-1]["parent"], second["control"]["parent"])

    def test_skip_then_plan_then_resume_keeps_skip_and_avoids_media(self):
        self.args.json = self.args.no_input = False
        def choose(context):
            return {"plan_id": context["plan_id"], "decision_id": context["decision_id"], "action": "skip", "params": {}}
        result, code = self.run_flow(Images(bad={"synthetic-1"}), decision_provider=choose)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["run"]["counts"]["skipped"], 1)
        self.assertEqual(result["errors"], [])
        skipped = next(item for item in result["downloads"] if item["status"] == "skipped")
        self.assertEqual(skipped["skip_reason"]["code"], "ASSET_VALIDATION_FAILED")
        self.assertNotIn("error", skipped)
        self.assertEqual(result["coverage"]["completeness"]["status"], "incomplete")
        self.assertIn("explicitly_skipped_assets", result["coverage"]["completeness"]["unresolved"])
        self.assertEqual(result["coverage"]["download"]["failed_asset_ids"], [])
        self.args.command, self.args.json, self.args.no_input = "plan", True, True
        planned, code = self.run_flow(NoNetwork())
        self.assertEqual(code, 0, planned)
        self.assertEqual(sorted(item["action"] for item in planned["data"]["items"]), ["reuse", "skip"])
        self.assertEqual(planned["data"]["decisions"], [])
        self.args = Namespace(command="resume", task_dir=str(self.root / "task"), json=True, no_input=True)
        resumed, code = self.run_flow(NoNetwork())
        self.assertEqual(code, 0, resumed)
        self.assertEqual(resumed["errors"], [])
        self.assertEqual(resumed["run"]["counts"]["skipped"], 1)
        self.assertEqual(resumed["run"]["counts"]["reused"], 1)

    def test_library_reservation_crash_reuses_the_same_version_in_actual_download(self):
        self.args.output, self.args.library = None, str(self.root / "library")
        from apple_music_artwork import library
        class Crash(BaseException):
            pass
        with patch.object(library, "_local_record", side_effect=Crash):
            with self.assertRaises(Crash):
                self.run_flow(NoNetwork())
        result, code = self.run_flow()
        self.assertEqual(code, 0, result)
        records = library.list_tasks(self.root / "library")
        self.assertEqual([record["version"] for record in records], [1])

    def test_each_timeout_or_eof_prompts_only_once_for_all_outstanding_items(self):
        self.args.json = self.args.no_input = False
        calls = []
        def choose(context):
            calls.append(context)
            return None
        result, code = self.run_flow(Images(bad={"synthetic-0", "synthetic-1"}), decision_provider=choose)
        self.assertEqual(code, 8, result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result["control"]["decisions"]), 2)

    def test_progress_snapshots_have_matching_counts_rows_and_one_event_sequence(self):
        events = []
        result, code = self.run_flow(event_sink=events.append)
        self.assertEqual(code, 0, result)
        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(len({event["run_id"] for event in events}), 1)
        self.assertEqual(events[0]["run_id"], result["run"]["run_id"])
        snapshots = [event for event in events if event["kind"] == "snapshot"]
        self.assertTrue(snapshots)
        for event in snapshots:
            self.assertEqual(event["active"], len(event["items"]))
            self.assertEqual(event["counts"]["total"], sum(value for key, value in event["counts"].items() if key != "total"))
            for item in event["items"]:
                self.assertIn("name_key", item)
                self.assertIn("width", item["spec"])
        self.assertEqual(snapshots[-1]["counts"]["completed"], 2)
        self.assertEqual(snapshots[-1]["active"], 0)

    def test_list_events_also_have_versioned_single_stream_identity(self):
        self.args.list = True
        self.args.all_static = False
        events = []
        result, code = self.run_flow(NoNetwork(), event_sink=events.append)
        self.assertEqual(code, 0, result)
        self.assertTrue(events)
        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(len({event["run_id"] for event in events}), 1)
        self.assertTrue(all(event["schema_version"] == 1 for event in events))
        self.assertEqual(events[0]["run_id"], result["run"]["run_id"])

    def test_saved_plan_cannot_ignore_explicit_new_version(self):
        self.args.command = "plan"
        planned, code = self.run_flow(NoNetwork())
        saved = self.root / "plan.json"
        saved.write_text(dumps_json(planned), encoding="utf-8")
        self.args = Namespace(command="download", plan_file=str(saved), new_version=True,
                              explicit_options=["new_version"], json=True, no_input=True)
        result, code = self.run_flow(NoNetwork())
        self.assertEqual(code, 2, result)
        self.assertEqual(result["errors"][0]["code"], "PLAN_STALE")

    def test_diagnostics_summary_reports_final_state_save_failure(self):
        with patch("apple_music_artwork.run_state.RunState.save_manifest", side_effect=ArtworkError("RUN_STATE_WRITE_FAILED", "injected", "download")):
            result, code = self.run_flow(Images(bad={"synthetic-1"}))
        self.assertEqual(code, 6, result)
        self.args = Namespace(command="diagnostics", task_dir=str(self.root / "task"), json=True)
        diagnostics, code = self.run_flow(NoNetwork())
        self.assertEqual(code, 0, diagnostics)
        self.assertEqual(diagnostics["data"]["summary"]["result"]["lifecycle"], "failed")
