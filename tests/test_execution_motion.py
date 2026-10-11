"""Real HLS metadata plus coordinator recovery; media preparation is synthetic.

The optional parser is required. These tests do not certify video decoding;
the separate media-tool integration owns that acceptance evidence.
"""

from argparse import Namespace
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from io import StringIO
import os
import signal
import tempfile
from unittest import TestCase
from unittest.mock import patch

from apple_music_artwork.client import HttpClient, HttpResponse
from apple_music_artwork.cli import main
from apple_music_artwork.execution import execute
from apple_music_artwork.errors import ArtworkError
from apple_music_artwork.download import OutputDirectory
from apple_music_artwork.motion import MediaTools
from apple_music_artwork.jsonio import dumps_json, loads_json
from apple_music_artwork.run_state import STATE_DIRECTORY, RunState
from apple_music_artwork.workflow import dispatch
from test_hls import MEDIA, variant
from test_motion import FakeRunner, METADATA


ORIGIN = "https://mvod.itunes.apple.com/synthetic/"
BODY = b"0123456789"


def payload(two=False):
    videos = {"motionDetailSquare": {"video": ORIGIN + "first-master.m3u8"}}
    if two:
        videos["motionSquareVideo1x1"] = {"video": ORIGIN + "second-master.m3u8"}
    return {"data": [{"id": "1", "type": "albums", "href": "/v1/catalog/us/albums/1",
                       "attributes": {"editorialVideo": videos}}]}


class Metadata:
    def __init__(self, *, fail=None, first_width=1920, forbid_get=False):
        self.fail, self.first_width, self.forbid_get = fail, first_width, forbid_get
        self.calls = []

    def request(self, url, **kwargs):
        self.calls.append(("GET", url))
        if self.forbid_get:
            raise AssertionError("Completed choices must not refresh their master/media playlists")
        if self.fail == "master" and url.endswith("first-master.m3u8"):
            return HttpResponse(400, {}, b"")
        if self.fail == "media" and url.endswith("first-media.m3u8"):
            return HttpResponse(400, {}, b"")
        if url.endswith("master.m3u8"):
            first = "first-master" in url
            width = self.first_width if first else 1920
            text = "#EXTM3U\n" + variant("first-media.m3u8" if first else "second-media.m3u8",
                                          resolution=f"{width}x1080")
        else:
            text = MEDIA.replace("movie.mp4", "first.mp4" if "first-media" in url else "second.mp4")
        return HttpResponse(200, {"Content-Type": "application/vnd.apple.mpegurl"}, text.encode())

    def head(self, url, **kwargs):
        self.calls.append(("HEAD", url))
        if self.fail == "head" and url.endswith("first.mp4"):
            return HttpResponse(400, {}, b"")
        if not url.endswith("shared.mp4"):
            return HttpResponse(302, {"Location": ORIGIN + "shared.mp4"}, b"")
        return HttpResponse(200, {"Content-Length": "10", "Content-Type": "video/mp4"}, b"")


def prepared(asset, inspection, temporary, http, tools, **kwargs):
    validation = {"decoded": True, "full_decode": True, "actual_format": "MP4",
                  "actual_dimensions": {"width": inspection.selected.width, "height": inspection.selected.height},
                  "actual_codec": inspection.selected.codec, "bytes": len(BODY), "sha256": sha256(BODY).hexdigest()}
    temporary.write_stream(lambda stream: stream.write(BODY), lambda path, transfer: validation)
    return {"asset_id": asset.asset_id, "path": temporary.final_name, "status": "downloaded", "kind": "video",
            "semantic_path": asset.semantic_path, "url": inspection.layout.mp4_url, "profile": inspection.profile,
            "layout": inspection.layout.to_dict(), "selected_variant": inspection.selected.to_dict(),
            "validation": validation}


class MotionRecoveryTests(TestCase):
    @classmethod
    def setUpClass(cls):
        runtime = Path(tempfile.gettempdir()).resolve()
        assert runtime.resolve(strict=True) == runtime
        cls.scope = Path(tempfile.mkdtemp(prefix="v03-core-motion-recovery-", dir=runtime))

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="case-", dir=self.scope))
        self.output, self.response = self.folder / "output", self.folder / "response.json"
        self.response.write_text(dumps_json(payload()), encoding="utf-8")
        self.args = Namespace(url="https://music.apple.com/us/album/synthetic/1?l=en-US", response=str(self.response),
                              output=str(self.output), list=False, semantic=None, asset_id=None, all_static=False,
                              all_media=False, all_motion=True, inspect_motion=False, compare_storefronts=None,
                              token_env="APPLE_MUSIC_DEVELOPER_TOKEN", provider="web", width=None, height=None,
                              format=None, motion_profile="best", jobs=2)
        self.tools = SimpleNamespace(to_dict=lambda: {"ffmpeg": {"version": "synthetic"}, "ffprobe": {"version": "synthetic"}})

    def run_task(self, transport, prepare=prepared):
        with patch("apple_music_artwork.motion.prepare_motion", side_effect=prepare) as worker:
            result, code = execute(self.args, http=HttpClient(transport), environ={}, media_tools=self.tools)
        return result, code, worker.call_count

    def old_video_policy(self):
        with OutputDirectory(self.output) as output, RunState(output) as state:
            record = next(iter(state.data["files"].values()))
            record["validation_policy"] = "mp4-packet-full-decode-v1"
            state.save()
            return deepcopy(record)

    def video_record(self):
        with OutputDirectory(self.output) as output, RunState(output) as state:
            return deepcopy(next(iter(state.data["files"].values())))

    def local_tools(self, **kwargs):
        metadata = deepcopy(METADATA)
        metadata["format"]["size"] = str(len(BODY))
        metadata["streams"][0].update(width=1920, height=1080)
        runner = FakeRunner(metadata, **kwargs)
        self.tools = MediaTools("/synthetic/ffmpeg", "/synthetic/ffprobe", "synthetic", "synthetic", runner)
        return runner

    def test_completed_old_video_policy_revalidates_once_and_keeps_original_evidence(self):
        first, code, _ = self.run_task(Metadata())
        self.assertEqual(code, 0, first["errors"])
        old = self.old_video_policy()
        runner = self.local_tools()
        transport = Metadata(forbid_get=True)
        result, code, count = self.run_task(transport, prepare=AssertionError("no redownload"))
        self.assertEqual(code, 0, result["errors"])
        self.assertEqual(transport.calls, [])
        self.assertEqual(count, 0)
        self.assertEqual(len(runner.calls), 2)
        entry = result["downloads"][0]
        self.assertEqual(entry["action"], "reused")
        self.assertEqual(entry["validation"], old["artifact"]["validation"])
        verification = entry["local_verification"]
        self.assertTrue(verification["decoded_this_run"])
        self.assertTrue(verification["validation"]["full_decode"])
        self.assertEqual(verification["validation_policy"], "mp4-packet-full-decode-v2")
        record = self.video_record()
        self.assertEqual(record["artifact"], old["artifact"])
        self.assertEqual(record["validation_policy"], "mp4-packet-full-decode-v2")

        runner.calls.clear()
        for _ in range(2):
            reused, code, count = self.run_task(transport, prepare=AssertionError("no redownload"))
            self.assertEqual(code, 0, reused["errors"])
            self.assertEqual(transport.calls, [])
            self.assertEqual(runner.calls, [])
            local = reused["downloads"][0]["local_verification"]
            self.assertFalse(local["decoded_this_run"])
            self.assertEqual(local["previous_full_validation"], verification)
            self.assertEqual(self.video_record()["local_verification"], local)
            self.assertEqual(self.video_record()["artifact"], old["artifact"])

    def test_prepared_old_video_policy_publishes_local_revalidation_evidence(self):
        class InterruptedPublication(BaseException):
            pass

        original = OutputDirectory.publish_temp

        def interrupted(directory, temporary, **kwargs):
            if temporary.final_name.endswith(".mp4"):
                raise InterruptedPublication()
            return original(directory, temporary, **kwargs)

        with patch.object(OutputDirectory, "publish_temp", interrupted):
            with self.assertRaises(InterruptedPublication):
                self.run_task(Metadata())
        old = self.old_video_policy()
        self.assertEqual(old["status"], "prepared")
        runner = self.local_tools()
        transport = Metadata(forbid_get=True)
        result, code, count = self.run_task(transport, prepare=AssertionError("no redownload"))
        self.assertEqual(code, 0, result["errors"])
        self.assertEqual(transport.calls, [])
        self.assertEqual(count, 0)
        self.assertEqual(len(runner.calls), 2)
        entry = result["downloads"][0]
        self.assertEqual(entry["validation"], old["artifact"]["validation"])
        verification = entry["local_verification"]
        self.assertTrue(verification["decoded_this_run"])
        self.assertTrue(verification["validation"]["full_decode"])
        self.assertEqual(self.video_record()["local_verification"], verification)
        self.assertEqual(self.video_record()["validation_policy"], "mp4-packet-full-decode-v2")
        self.assertEqual((self.output / entry["path"]).read_bytes(), BODY)
        runner.calls.clear()
        reused, code, count = self.run_task(transport, prepare=AssertionError("no redownload"))
        self.assertEqual(code, 0, reused["errors"])
        self.assertEqual(runner.calls, [])
        self.assertFalse(reused["downloads"][0]["local_verification"]["decoded_this_run"])
        self.assertEqual(reused["downloads"][0]["local_verification"]["previous_full_validation"], verification)

    def test_failed_old_policy_revalidation_preserves_completed_file_and_old_evidence(self):
        for failure in ({"probe_code": 1}, {"decode_code": 1}):
            with self.subTest(failure=failure):
                self.output = self.folder / next(iter(failure))
                self.args.output = str(self.output)
                first, code, _ = self.run_task(Metadata())
                self.assertEqual(code, 0, first["errors"])
                old = self.old_video_policy()
                runner = self.local_tools(**failure)
                transport = Metadata(forbid_get=True)
                result, code, count = self.run_task(transport, prepare=AssertionError("no redownload"))
                self.assertNotEqual(code, 0)
                self.assertEqual(transport.calls, [])
                self.assertEqual(count, 0)
                self.assertEqual(len(runner.calls), 1 if "probe_code" in failure else 2)
                expected = "VIDEO_PROBE_FAILED" if "probe_code" in failure else "VIDEO_DECODE_FAILED"
                self.assertEqual(result["downloads"][0]["error"]["code"], expected)
                record = self.video_record()
                self.assertEqual(record["validation_policy"], old["validation_policy"])
                self.assertEqual(record["artifact"], old["artifact"])
                self.assertNotIn("local_verification", record)
                self.assertEqual((self.output / first["downloads"][0]["path"]).read_bytes(), BODY)

    def test_failed_prepared_revalidation_keeps_temporary_bytes_and_old_evidence(self):
        class InterruptedPublication(BaseException):
            pass

        original = OutputDirectory.publish_temp

        def interrupted(directory, temporary, **kwargs):
            if temporary.final_name.endswith(".mp4"):
                raise InterruptedPublication()
            return original(directory, temporary, **kwargs)

        with patch.object(OutputDirectory, "publish_temp", interrupted):
            with self.assertRaises(InterruptedPublication):
                self.run_task(Metadata())
        old = self.old_video_policy()
        runner = self.local_tools(decode_code=1)
        transport = Metadata(forbid_get=True)
        result, code, count = self.run_task(transport, prepare=AssertionError("no redownload"))
        self.assertNotEqual(code, 0)
        self.assertEqual(result["downloads"][0]["error"]["code"], "VIDEO_DECODE_FAILED")
        self.assertEqual(transport.calls, [])
        self.assertEqual(count, 0)
        self.assertEqual(len(runner.calls), 2)
        record = self.video_record()
        self.assertEqual(record["validation_policy"], old["validation_policy"])
        self.assertEqual(record["artifact"], old["artifact"])
        self.assertEqual(record["temporary"], old["temporary"])
        self.assertNotIn("local_verification", record)
        self.assertEqual((self.output / old["temporary"]["name"]).read_bytes(), BODY)
        self.assertFalse((self.output / old["artifact"]["path"]).exists())
        failed_again, code, count = self.run_task(transport, prepare=AssertionError("no redownload"))
        self.assertNotEqual(code, 0)
        self.assertEqual(failed_again["downloads"][0]["error"]["code"], "VIDEO_DECODE_FAILED")
        self.assertEqual(count, 0)
        record = self.video_record()
        self.assertEqual(record["temporary"], old["temporary"])
        self.assertEqual(record["artifact"], old["artifact"])
        self.assertEqual(record["validation_policy"], old["validation_policy"])
        self.assertEqual((self.output / old["temporary"]["name"]).read_bytes(), BODY)
        runner = self.local_tools()
        recovered, code, count = self.run_task(transport, prepare=AssertionError("no redownload"))
        self.assertEqual(code, 0, recovered["errors"])
        self.assertEqual(count, 0)
        self.assertEqual(len(runner.calls), 2)
        self.assertTrue(recovered["downloads"][0]["local_verification"]["decoded_this_run"])
        self.assertEqual(self.video_record()["artifact"], old["artifact"])
        self.assertEqual(self.video_record()["validation_policy"], "mp4-packet-full-decode-v2")
        self.assertEqual((self.output / recovered["downloads"][0]["path"]).read_bytes(), BODY)
        runner.calls.clear()
        reused, code, count = self.run_task(transport, prepare=AssertionError("no redownload"))
        self.assertEqual(code, 0, reused["errors"])
        self.assertFalse(reused["downloads"][0]["local_verification"]["decoded_this_run"])
        self.assertEqual(runner.calls, [])
        self.assertEqual(transport.calls, [])

    def test_reuse_does_not_attribute_mismatched_prior_verification_to_current_file(self):
        first, code, _ = self.run_task(Metadata())
        self.assertEqual(code, 0, first["errors"])
        self.old_video_policy()
        runner = self.local_tools()
        transport = Metadata(forbid_get=True)
        verified, code, _ = self.run_task(transport)
        self.assertEqual(code, 0, verified["errors"])
        verification = verified["downloads"][0]["local_verification"]
        runner.calls.clear()
        for key, value in (("validation_policy", "mp4-packet-full-decode-v1"),
                           ("bytes", 11), ("sha256", "0" * 64)):
            with self.subTest(field=key):
                with OutputDirectory(self.output) as output, RunState(output) as state:
                    record = next(iter(state.data["files"].values()))
                    record["local_verification"] = deepcopy(verification)
                    record["local_verification"][key] = value
                    state.save()
                reused, code, _ = self.run_task(transport)
                self.assertEqual(code, 0, reused["errors"])
                self.assertFalse(reused["downloads"][0]["local_verification"]["decoded_this_run"])
                self.assertNotIn("previous_full_validation", reused["downloads"][0]["local_verification"])
                self.assertEqual(runner.calls, [])
                self.assertEqual(transport.calls, [])

    def test_interrupted_prepared_policy_upgrade_resumes_with_prior_evidence_without_decoding(self):
        class InterruptedPublication(BaseException):
            pass

        original = OutputDirectory.publish_temp

        def interrupted(directory, temporary, **kwargs):
            if temporary.final_name.endswith(".mp4"):
                raise InterruptedPublication()
            return original(directory, temporary, **kwargs)

        with patch.object(OutputDirectory, "publish_temp", interrupted):
            with self.assertRaises(InterruptedPublication):
                self.run_task(Metadata())
        old = self.old_video_policy()
        runner = self.local_tools()
        transport = Metadata(forbid_get=True)
        with patch.object(OutputDirectory, "publish_temp", interrupted):
            with self.assertRaises(InterruptedPublication):
                self.run_task(transport)
        verified = self.video_record()
        self.assertEqual(verified["status"], "prepared")
        self.assertEqual(verified["validation_policy"], "mp4-packet-full-decode-v2")
        self.assertEqual(len(runner.calls), 2)
        runner.calls.clear()
        result, code, count = self.run_task(transport, prepare=AssertionError("no redownload"))
        self.assertEqual(code, 0, result["errors"])
        self.assertEqual(count, 0)
        self.assertEqual(runner.calls, [])
        self.assertEqual(transport.calls, [])
        local = result["downloads"][0]["local_verification"]
        self.assertFalse(local["decoded_this_run"])
        self.assertEqual(local["previous_full_validation"], verified["local_verification"])
        self.assertEqual(self.video_record()["local_verification"], local)
        self.assertEqual(self.video_record()["artifact"], old["artifact"])

    def test_malformed_optional_verification_is_rejected_without_changing_media(self):
        first, code, _ = self.run_task(Metadata())
        self.assertEqual(code, 0, first["errors"])
        transport = Metadata(forbid_get=True)
        runner = self.local_tools()
        for malformed in ([], {"decoded_this_run": False, "previous_full_validation": []}):
            with self.subTest(malformed=malformed):
                with OutputDirectory(self.output) as output, RunState(output) as state:
                    record = next(iter(state.data["files"].values()))
                    record["local_verification"] = malformed
                    state.save()
                result, code, _ = self.run_task(transport)
                self.assertNotEqual(code, 0)
                self.assertEqual(result["errors"][0]["code"], "RUN_STATE_INVALID")
                self.assertEqual((self.output / first["downloads"][0]["path"]).read_bytes(), BODY)
                self.assertEqual(self.video_record()["local_verification"], malformed)
                self.assertEqual(transport.calls, [])
                self.assertEqual(runner.calls, [])

    def workflow_task(self, args, transport, *, prepare=prepared, decision_provider=None):
        with patch("apple_music_artwork.motion.prepare_motion", side_effect=prepare) as worker:
            result, code = dispatch(args, http=HttpClient(transport), environ={}, media_tools=self.tools,
                                    decision_provider=decision_provider)
        return result, code, worker.call_count

    def old_workflow_video(self, *, sealed):
        self.output = self.folder / ("prepared" if sealed else "committed")
        args = Namespace(command="download", url=self.args.url, response=str(self.response),
                         output=str(self.output), all_motion=True, no_input=True, json=True)
        if sealed:
            class InterruptedPublication(BaseException):
                pass

            original = OutputDirectory.publish_temp

            def interrupted(directory, temporary, **kwargs):
                if temporary.final_name.endswith(".mp4"):
                    raise InterruptedPublication()
                return original(directory, temporary, **kwargs)

            with patch.object(OutputDirectory, "publish_temp", interrupted):
                with self.assertRaises(InterruptedPublication):
                    self.workflow_task(args, Metadata())
        else:
            first, code, _ = self.workflow_task(args, Metadata())
            self.assertEqual(code, 0, first)
        old = self.old_video_policy()
        filename = old["temporary"]["name"] if sealed else old["artifact"]["path"]
        # Workflow tasks use the v2 videos/ layout; artifact paths are media-relative.
        media = self.output / "videos" / filename
        self.assertEqual(media.read_bytes(), BODY)
        return old, media

    def test_interactive_retry_of_old_video_revalidates_locally_without_losing_evidence(self):
        for sealed in (False, True):
            with self.subTest(sealed=sealed):
                old, media = self.old_workflow_video(sealed=sealed)
                runner = self.local_tools(decode_code=1)
                transport = Metadata(forbid_get=True)
                resume = Namespace(command="resume", task_dir=str(self.output), no_input=False, json=False)
                reasons = []

                def choose(context):
                    reasons.append(context["reason"])
                    if len(reasons) == 1:
                        return {"plan_id": context["plan_id"], "decision_id": context["decision_id"],
                                "action": "retry", "params": {}}
                    return None

                rejected, code, count = self.workflow_task(resume, transport,
                    prepare=ArtworkError("SYNTHETIC_REDOWNLOAD", "Download must not run for local verification.", "download"),
                    decision_provider=choose)
                self.assertEqual(count, 0)
                self.assertEqual(code, 8, rejected)
                self.assertEqual(reasons, ["VIDEO_DECODE_FAILED", "VIDEO_DECODE_FAILED"])
                self.assertEqual(len(runner.calls), 4)
                self.assertEqual(transport.calls, [])
                retained = self.video_record()
                for field in ("artifact", "validation_policy", "temporary"):
                    self.assertEqual(retained[field], old[field])
                self.assertEqual(media.read_bytes(), BODY)
                runner.calls.clear()
                reasons.clear()

                def recover(context):
                    reasons.append(context["reason"])
                    runner.decode_code = 0
                    return {"plan_id": context["plan_id"], "decision_id": context["decision_id"],
                            "action": "retry", "params": {}} if len(reasons) == 1 else None

                recovered, code, count = self.workflow_task(resume, transport,
                    prepare=AssertionError("no redownload"), decision_provider=recover)
                self.assertEqual(code, 0, recovered)
                self.assertEqual(count, 0)
                self.assertEqual(reasons, ["VIDEO_DECODE_FAILED"])
                self.assertEqual(len(runner.calls), 4)
                self.assertTrue(recovered["downloads"][0]["local_verification"]["decoded_this_run"])
                self.assertEqual(self.video_record()["artifact"], old["artifact"])
                self.assertEqual(self.video_record()["validation_policy"], "mp4-packet-full-decode-v2")
                self.assertEqual((self.output / recovered["downloads"][0]["path"]).read_bytes(), BODY)
                runner.calls.clear()
                reused, code, count = self.workflow_task(resume, transport, prepare=AssertionError("no redownload"))
                self.assertEqual(code, 0, reused)
                self.assertEqual(runner.calls, [])
                self.assertEqual(transport.calls, [])
                self.assertFalse(reused["downloads"][0]["local_verification"]["decoded_this_run"])

    def test_protocol_retry_of_old_video_uses_local_validation_for_both_file_states(self):
        for sealed in (False, True):
            with self.subTest(sealed=sealed):
                old, media = self.old_workflow_video(sealed=sealed)
                runner = self.local_tools(decode_code=1)
                transport = Metadata(forbid_get=True)
                resume = Namespace(command="resume", task_dir=str(self.output), no_input=True, json=True)
                blocked, code, _ = self.workflow_task(resume, transport, prepare=AssertionError("no redownload"))
                self.assertEqual(code, 8, blocked)
                choices = self.folder / ("prepared-choices.json" if sealed else "committed-choices.json")

                def save_retry(result):
                    context = result["control"]["decisions"][0]
                    self.assertEqual(context["reason"], "VIDEO_DECODE_FAILED")
                    choices.write_text(dumps_json({"protocol_version": 1, "choices": [{
                        "plan_id": context["plan_id"], "decision_id": context["decision_id"],
                        "action": "retry", "params": {}}]}), encoding="utf-8")

                save_retry(blocked)
                resume.decisions_file = str(choices)
                runner.calls.clear()
                rejected, code, count = self.workflow_task(resume, transport,
                    prepare=ArtworkError("SYNTHETIC_REDOWNLOAD", "Download must not run for local verification.", "download"))
                self.assertEqual(code, 8, rejected)
                self.assertEqual(count, 0)
                self.assertEqual(len(runner.calls), 4)
                self.assertEqual(transport.calls, [])
                retained = self.video_record()
                for field in ("artifact", "validation_policy", "temporary"):
                    self.assertEqual(retained[field], old[field])
                self.assertEqual(media.read_bytes(), BODY)
                save_retry(rejected)
                runner.calls.clear()
                original_run = runner.run

                def recover_after_initial_decode(argv, **limits):
                    result = original_run(argv, **limits)
                    if "-progress" in argv:
                        runner.decode_code = 0
                    return result

                runner.run = recover_after_initial_decode
                recovered, code, count = self.workflow_task(resume, transport, prepare=AssertionError("no redownload"))
                self.assertEqual(code, 0, recovered)
                self.assertEqual(count, 0)
                self.assertEqual(len(runner.calls), 4)
                self.assertTrue(recovered["downloads"][0]["local_verification"]["decoded_this_run"])
                self.assertEqual(self.video_record()["artifact"], old["artifact"])
                self.assertEqual(self.video_record()["validation_policy"], "mp4-packet-full-decode-v2")
                self.assertEqual((self.output / recovered["downloads"][0]["path"]).read_bytes(), BODY)
                resume.decisions_file = None
                runner.calls.clear()
                reused, code, count = self.workflow_task(resume, transport, prepare=AssertionError("no redownload"))
                self.assertEqual(code, 0, reused)
                self.assertEqual(runner.calls, [])
                self.assertEqual(transport.calls, [])
                self.assertFalse(reused["downloads"][0]["local_verification"]["decoded_this_run"])

    def test_first_incomplete_inspection_resumes_only_unknown_metadata(self):
        for failure in ("master", "media", "head"):
            self.args.output = str(self.folder / failure)
            first, code, prepared_count = self.run_task(Metadata(fail=failure))
            self.assertEqual(code, 6)
            self.assertEqual(first["run"]["counts"]["total"], 0)
            self.assertEqual(prepared_count, 0)
            transport = Metadata()
            result, code, prepared_count = self.run_task(transport)
            self.assertEqual(code, 0, result["errors"])
            self.assertEqual(prepared_count, 1)
            self.assertEqual(result["run"]["snapshot"]["remote_freshness"], "not_rechecked")
            self.assertEqual(result["run"]["pending_inspection"]["completed"], 1)
            gets = [url for method, url in transport.calls if method == "GET"]
            self.assertEqual(gets, [ORIGIN + "first-master.m3u8", ORIGIN + "first-media.m3u8"] if failure == "master"
                             else [ORIGIN + "first-media.m3u8"] if failure == "media" else [])

    def test_new_same_mp4_alias_merges_into_previously_completed_file_without_transfer(self):
        self.response.write_text(dumps_json(payload(two=True)), encoding="utf-8")
        first, code, count = self.run_task(Metadata(fail="head"))
        self.assertEqual(code, 6, first["errors"])
        self.assertEqual(count, 1)
        saved = next(item for item in first["downloads"] if item["status"] == "downloaded")
        transport = Metadata(forbid_get=True)
        result, code, count = self.run_task(transport, prepare=AssertionError("same MP4 must not be fetched again"))
        self.assertEqual(code, 0, result["errors"])
        self.assertEqual(count, 0)
        self.assertEqual(result["run"]["counts"]["total"], 1)
        self.assertEqual(result["run"]["counts"]["reused"], 1)
        self.assertEqual(len(result["downloads"][0]["references"]), 2)
        self.assertEqual(result["downloads"][0]["path"], saved["path"])
        self.assertEqual((self.output / saved["path"]).read_bytes(), BODY)
        self.assertTrue(all(method == "HEAD" for method, _ in transport.calls))

    def test_new_alias_with_conflicting_constraints_preserves_completed_file(self):
        self.response.write_text(dumps_json(payload(two=True)), encoding="utf-8")
        first, code, count = self.run_task(Metadata(fail="head", first_width=1280))
        saved = next(item for item in first["downloads"] if item["status"] == "downloaded")
        result, code, count = self.run_task(Metadata(forbid_get=True), prepare=AssertionError("no transfer"))
        self.assertEqual(code, 6, result["errors"])
        self.assertEqual(count, 0)
        self.assertEqual(result["run"]["counts"]["total"], 1)
        self.assertEqual(result["run"]["counts"]["failed"], 1)
        self.assertEqual(result["errors"][0]["code"], "MOTION_DECLARATION_CONFLICT")
        self.assertEqual((self.output / saved["path"]).read_bytes(), BODY)

    def test_legacy_best_selected_lower_than_frozen_variants_is_rejected_before_io(self):
        self.args.response = None
        with patch("apple_music_artwork.execution.AppleMusicClient.fetch", return_value=payload()):
            first, code, count = self.run_task(Metadata())
        self.assertEqual(code, 0, first["errors"])
        old = loads_json((self.output / "manifest.json").read_bytes())
        old.pop("run")
        inspection = old["motion_inspections"][0]["inspection"]
        higher = deepcopy(inspection["selected"])
        higher.update(width=3840, height=2160, url=ORIGIN + "higher-media.m3u8")
        inspection["variants"].append(higher)
        legacy = self.folder / "legacy-low-selected"
        legacy.mkdir()
        original = dumps_json(old).encode()
        (legacy / "manifest.json").write_bytes(original)
        self.args.output = str(legacy)
        transport = Metadata(forbid_get=True)
        result, code, count = self.run_task(transport)
        self.assertEqual(code, 6)
        self.assertEqual(result["errors"][0]["code"], "LEGACY_MANIFEST_UNRECOGNIZED")
        self.assertEqual(transport.calls, [])
        self.assertEqual(count, 0)
        self.assertEqual((legacy / "manifest.json").read_bytes(), original)

    def test_cli_list_sigint_preserves_partial_inspection_and_stops_other_sources(self):
        self.response.write_text(dumps_json(payload(two=True)), encoding="utf-8")

        class Interrupted(Metadata):
            def request(self, url, **kwargs):
                os.kill(os.getpid(), signal.SIGINT)
                # The metadata call runs in its serial slot. Do not race the
                # main thread's actual signal delivery with a zero-time stub.
                from threading import Event
                while not self.control.cancelled:
                    Event().wait(0.01)
                return super().request(url, **kwargs)

        transport, stdout, stderr = Interrupted(), StringIO(), StringIO()
        http = HttpClient(transport)
        transport.control = http.control
        code = main([self.args.url, "--response", str(self.response), "--list", "--inspect-motion",
                     "--progress", "never", "--json"], http=http, environ={}, stdout=stdout, stderr=stderr)
        result = loads_json(stdout.getvalue())
        self.assertEqual(code, 130)
        self.assertTrue(result["run"]["cancelled"])
        self.assertEqual(result["run"]["stats"]["requests"], 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(result["motion_inspections"]), 1)
        self.assertEqual(result["motion_inspections"][0]["error"]["code"], "CANCELLED")
        self.assertEqual(len(result["assets"]), 2)
        self.assertEqual(stderr.getvalue(), "")

    def test_hls_retry_deadline_keeps_wait_evidence_in_json_and_human_error(self):
        class RateLimited(Metadata):
            def head(self, url, **kwargs):
                self.calls.append(("HEAD", url))
                return HttpResponse(429, {"Retry-After": "600"}, b"")

        result, code, prepared_count = self.run_task(RateLimited())
        self.assertEqual(code, 6)
        self.assertEqual(prepared_count, 0)
        error = result["errors"][0]
        self.assertEqual(error["code"], "RETRY_AFTER_DEADLINE")
        self.assertEqual(error["retry_after_seconds"], 600)
        self.assertGreater(error["retry_at"], 0)
        self.assertEqual(result["downloads"][0]["error"]["retry_at"], error["retry_at"])
        stdout, stderr = StringIO(), StringIO()
        code = main([self.args.url, "--response", str(self.response), "--all-motion", "--progress", "never",
                     "-o", str(self.folder / "human-output")], transport=RateLimited(), environ={},
                    media_tools=self.tools, stdout=stdout, stderr=stderr)
        self.assertEqual(code, 8)
        self.assertIn("服务要求等待 600 秒", stderr.getvalue())
        self.assertIn("可重试时间：", stderr.getvalue())

