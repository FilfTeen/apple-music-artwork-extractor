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
from apple_music_artwork.jsonio import dumps_json, loads_json
from apple_music_artwork.run_state import STATE_DIRECTORY
from test_hls import MEDIA, variant


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

