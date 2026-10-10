"""Recovery boundaries through actual workflow entry points; synthetic media only."""

from argparse import Namespace
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from apple_music_artwork.client import HttpClient, HttpResponse
from apple_music_artwork.jsonio import dumps_json
from apple_music_artwork.workflow import dispatch
from test_execution_motion import Metadata, payload, prepared


class MotionDecisionTests(TestCase):
    def test_skipped_hls_issue_stays_skipped_in_plan_and_resume_without_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source.json"
            source.write_text(dumps_json(payload()), encoding="utf-8")
            args = Namespace(command="download", url="https://music.apple.com/us/album/synthetic/1?l=en-US",
                             response=str(source), output=str(root / "task"), all_motion=True, no_input=False, json=False)
            tools = SimpleNamespace(to_dict=lambda: {"ffmpeg": {"version": "synthetic"}, "ffprobe": {"version": "synthetic"}})
            def choose(context):
                return {"plan_id": context["plan_id"], "decision_id": context["decision_id"], "action": "skip", "params": {}}
            first, code = dispatch(args, http=HttpClient(Metadata(fail="master")), environ={}, media_tools=tools,
                                   decision_provider=choose)
            self.assertEqual(code, 0, first)
            self.assertEqual(first["downloads"][0]["status"], "skipped")
            self.assertEqual(first["errors"], [])
            self.assertIn("skip_reason", first["downloads"][0])
            self.assertNotIn("error", first["downloads"][0])
            self.assertIn("explicitly_skipped_assets", first["coverage"]["completeness"]["unresolved"])
            self.assertEqual(first["run"]["counts"]["skipped_issues"], 1)
            transport = Metadata(forbid_get=True)
            args.command, args.json, args.no_input = "plan", True, True
            planned, code = dispatch(args, http=HttpClient(transport), environ={}, media_tools=tools)
            self.assertEqual(code, 0, planned)
            self.assertEqual(planned["data"]["items"][0]["action"], "skip")
            self.assertEqual(planned["data"]["decisions"], [])
            resume = Namespace(command="resume", task_dir=str(root / "task"), no_input=True, json=True)
            resumed, code = dispatch(resume, http=HttpClient(transport), environ={}, media_tools=tools)
            self.assertEqual(code, 0, resumed)
            self.assertEqual(resumed["errors"], [])
            self.assertEqual(resumed["downloads"][0]["status"], "skipped")
            self.assertEqual(transport.calls, [])

    def test_frozen_hls_issue_obeys_persisted_cooldown_before_any_fresh_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source.json"
            source.write_text(dumps_json(payload()), encoding="utf-8")
            args = Namespace(command="download", url="https://music.apple.com/us/album/synthetic/1?l=en-US",
                             response=str(source), output=str(root / "task"), all_motion=True, no_input=True, json=True)
            tools = SimpleNamespace(to_dict=lambda: {"ffmpeg": {"version": "synthetic"}, "ffprobe": {"version": "synthetic"}})
            class Limited(Metadata):
                def head(self, url, **kwargs):
                    self.calls.append(("HEAD", url))
                    return HttpResponse(429, {"Retry-After": "600"}, b"")
            def client(transport, now):
                return HttpClient(transport, clock=lambda: now, monotonic=lambda: 0,
                                  sleep=lambda _: self.fail("A wait exceeding the operation window must not sleep"))
            first, code = dispatch(args, http=client(Limited(), 1000), environ={}, media_tools=tools)
            self.assertEqual(code, 8, first)
            context = first["control"]["decisions"][0]
            self.assertEqual(context["retry_at"], 1600)
            choices = root / "choices.json"
            def choose(context):
                choices.write_text(dumps_json({"protocol_version": 1, "choices": [{"plan_id": context["plan_id"],
                    "decision_id": context["decision_id"], "action": "retry", "params": {}}]}), encoding="utf-8")
            choose(context)
            resume = Namespace(command="resume", task_dir=str(root / "task"), decisions_file=str(choices), no_input=True, json=True)
            early_transport = Metadata()
            early, code = dispatch(resume, http=client(early_transport, 1000), environ={}, media_tools=tools)
            self.assertEqual(code, 8, early)
            self.assertEqual(early_transport.calls, [])
            self.assertEqual(early["control"]["decisions"][0]["retry_at"], 1600)
            choose(early["control"]["decisions"][0])
            after_transport = Metadata()
            with patch("apple_music_artwork.motion.prepare_motion", side_effect=prepared):
                done, code = dispatch(resume, http=client(after_transport, 1600), environ={}, media_tools=tools)
            self.assertEqual(code, 0, done)
            self.assertTrue(after_transport.calls)
            self.assertEqual(done["run"]["counts"]["new_downloaded"], 1)
