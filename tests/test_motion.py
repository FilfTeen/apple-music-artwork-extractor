"""Offline media seams and owned output files; fake probes are not live proof."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
from threading import Event
from unittest import TestCase, skipUnless
from unittest.mock import patch

from apple_music_artwork.download import OutputCommitError, OutputDirectory
from apple_music_artwork.errors import ArtworkError
from apple_music_artwork.hls import MotionInspection, MotionLayout, MotionVariant
from apple_music_artwork.models import ResourceKey, VisualAsset
from apple_music_artwork.motion import MediaTools, ProcessResult, ProcessRunner, download_motion, motion_filename, prepare_motion, validate_motion
from apple_music_artwork.runtime import RuntimeControl


BODY = b"synthetic-media-only-for-fake-decoder-tests"
ASSET = VisualAsset(ResourceKey("us", "albums", "1"), "editorialVideo.motionSquare", "video", "motion",
                    "https://mvod.itunes.apple.com/synthetic/master.m3u8", "synthetic")
VARIANT = MotionVariant("https://mvod.itunes.apple.com/synthetic/media.m3u8", 4, 4, "hvc1.2.4.L120.B0", 30, 9000, 10000)
LAYOUT = MotionLayout("https://mvod.itunes.apple.com/synthetic/movie.mp4", "https://mvod.itunes.apple.com/synthetic/movie.mp4",
                      VARIANT.url, len(BODY), 2, 1, ((0, 4), (4, len(BODY) - 4)))
INSPECTION = MotionInspection(ASSET.asset_id, ASSET.url, status="supported", variants=(VARIANT,), selected=VARIANT, layout=LAYOUT)
METADATA = {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "2.0", "start_time": "0.0", "size": str(len(BODY))},
            "streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 4, "height": 4,
                         "avg_frame_rate": "30/1", "r_frame_rate": "30/1", "duration": "2.0", "pix_fmt": "yuv420p",
                         "time_base": "1/30", "start_pts": 0, "start_time": "0.0", "duration_ts": 60}],
            "packets": [{"stream_index": 0, "pts": index, "dts": index, "duration": 1, "flags": "___"}
                        for index in range(60)]}


def decode_progress(frames=60, microseconds=2_000_000, dup=0, drop=0):
    return f"frame={frames}\nout_time_us={microseconds}\ndup_frames={dup}\ndrop_frames={drop}\nprogress=end\n".encode()


class FakeRunner:
    def __init__(self, metadata=None, *, probe_code=0, decode_code=0, progress=None):
        self.metadata = deepcopy(METADATA if metadata is None else metadata)
        self.probe_code = probe_code
        self.decode_code = decode_code
        self.progress = decode_progress() if progress is None else progress
        self.calls = []

    def run(self, argv, **limits):
        self.calls.append((argv, limits))
        if argv[1:] == ["-version"]:
            name = "ffmpeg" if len(self.calls) == 1 else "ffprobe"
            return ProcessResult(0, f"{name} version synthetic-1 Copyright\n".encode(), b"")
        if "-show_entries" in argv:
            return ProcessResult(self.probe_code, json.dumps(self.metadata).encode(), b"private diagnostic")
        return ProcessResult(self.decode_code, self.progress, b"private diagnostic")


class FakeMediaClient:
    def __init__(self, failure=None):
        self.calls = 0
        self.failure = failure

    def stream_to(self, url, sink, **limits):
        self.calls += 1
        sink.write(BODY[:8])
        if self.failure:
            raise self.failure
        sink.write(BODY[8:])
        return {"url": url, "bytes": len(BODY), "sha256": sha256(BODY).hexdigest(), "content_type": "video/mp4"}


@skipUnless(os.name == "posix", "Safe file publication currently requires POSIX")
class MotionOutputTests(TestCase):
    @classmethod
    def setUpClass(cls):
        # Create synthetic fixtures inside this test class's temporary directory.
        # tempfile honors the test run's configured temporary root.
        cls.temporary_scope = tempfile.TemporaryDirectory(prefix="artwork-motion-")
        cls.addClassCleanup(cls.temporary_scope.cleanup)
        cls.scope = Path(cls.temporary_scope.name).resolve()

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="case-", dir=self.scope))

    def download(self, runner=None, client=None, inspection=INSPECTION):
        runner = runner or FakeRunner()
        client = client or FakeMediaClient()
        tools = MediaTools("/synthetic/ffmpeg", "/synthetic/ffprobe", "synthetic", "synthetic", runner)
        with OutputDirectory(self.folder) as output:
            return download_motion(ASSET, inspection, output, client, tools)

    def test_original_bytes_are_published_only_after_probe_and_full_decode(self):
        runner = FakeRunner()
        artifact = self.download(runner)
        self.assertEqual((self.folder / artifact["path"]).read_bytes(), BODY)
        self.assertEqual(artifact["validation"]["sha256"], sha256(BODY).hexdigest())
        self.assertTrue(artifact["validation"]["decoded"] and artifact["validation"]["full_decode"])
        self.assertFalse(artifact["validation"]["transcoded"])
        self.assertEqual(len(runner.calls), 2)
        for argv, limits in runner.calls:
            local = argv[argv.index("-i") + 1]
            self.assertTrue(Path(local).is_absolute())
            self.assertTrue(Path(local).name.endswith(".part"))
            self.assertNotIn("https://", " ".join(argv))
            self.assertEqual(argv[argv.index("-protocol_whitelist") + 1], "file")
            self.assertGreater(limits["max_output_bytes"], 0)
        self.assertFalse(list(self.folder.glob("*.part")))
        self.assertNotIn(str(self.folder), json.dumps(artifact))

    def test_failed_probe_or_full_decode_never_publishes_mp4(self):
        for runner in (FakeRunner(probe_code=1), FakeRunner(decode_code=1),
                       FakeRunner(progress=b"frame=0\nprogress=end\n"), FakeRunner(progress=b"frame=60\nprogress=continue\n")):
            with self.subTest(runner=runner), self.assertRaises(OutputCommitError) as failure:
                self.download(runner)
            self.assertEqual(failure.exception.category, "validation")
            self.assertFalse(failure.exception.committed)
            self.assertIsNone(failure.exception.artifact)
            self.assertEqual(list(self.folder.iterdir()), [])
            self.assertNotIn("private diagnostic", str(failure.exception))

    def test_metadata_codec_dimensions_frame_rate_duration_tracks_are_checked(self):
        cases = []
        for field, value, code in (("codec_name", "h264", "VIDEO_CODEC_MISMATCH"),
                                    ("width", 5, "ASSET_DIMENSION_MISMATCH"),
                                    ("avg_frame_rate", "60/1", "VIDEO_FRAME_RATE_MISMATCH"),
                                    ("r_frame_rate", "0/0", "VIDEO_FRAME_RATE_MISMATCH")):
            metadata = deepcopy(METADATA)
            metadata["streams"][0][field] = value
            cases.append((metadata, code))
        extra = deepcopy(METADATA)
        extra["streams"].append({"codec_type": "audio"})
        cases.append((extra, "VIDEO_TRACK_MISMATCH"))
        for metadata, code in cases:
            runner = FakeRunner(metadata)
            with self.subTest(code=code), self.assertRaises(ArtworkError) as failure:
                self.download(runner)
            self.assertEqual(failure.exception.code, code)
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(list(self.folder.iterdir()), [])

    def test_hls_frame_rate_is_a_maximum_not_exact_average(self):
        metadata = deepcopy(METADATA)
        metadata["streams"][0]["avg_frame_rate"] = "30000/1001"
        artifact = self.download(FakeRunner(metadata))
        self.assertLess(artifact["validation"]["frame_rate"]["average"], 30)

    def test_nonzero_fragment_timestamps_and_b_frames_use_presentation_span(self):
        # Reproduces the observed Apple layout: duration is a demuxer-declared
        # endpoint, and packet order is decoding order with reordered PTS.
        metadata = deepcopy(METADATA)
        metadata["format"].update(start_time="10.033333", duration="12.000000")
        metadata["streams"][0].update(start_pts=301, start_time="10.033333", duration_ts=360, duration="12.000000")
        order = [base + offset for base in range(0, 60, 3) for offset in (0, 2, 1)]
        metadata["packets"] = [{"stream_index": 0, "pts": 301 + presentation, "dts": 300 + index, "duration": 1}
                               for index, presentation in enumerate(order)]
        result = self.download(FakeRunner(metadata))["validation"]
        self.assertEqual(result["duration_seconds"], 2)
        self.assertEqual(result["duration_basis"], "complete_packet_presentation_span")
        self.assertEqual(result["packet_timeline"]["presentation_start_tick"], 301)
        self.assertEqual(result["packet_timeline"]["presentation_end_tick"], 361)
        self.assertEqual(result["timing_declarations"]["container"]["duration"], "12.000000")
        self.assertEqual(result["decode_endpoint_seconds"], 2)

    def test_proven_contiguous_vfr_is_allowed_without_constant_frame_spacing(self):
        metadata = deepcopy(METADATA)
        metadata["streams"][0].update(time_base="1/2", avg_frame_rate="3/2", r_frame_rate="2/1")
        metadata["packets"] = [{"stream_index": 0, "pts": 0, "dts": 0, "duration": 1},
                               {"stream_index": 0, "pts": 2, "dts": 1, "duration": 2},
                               {"stream_index": 0, "pts": 1, "dts": 3, "duration": 1}]
        result = self.download(FakeRunner(metadata, progress=decode_progress(frames=3)))["validation"]
        self.assertEqual(result["duration_seconds"], 2)
        self.assertEqual(result["decoded_frames"], 3)
        self.assertEqual(result["packet_timeline"]["packet_count"], 3)

    def test_missing_duplicate_or_invalid_packets_fail_before_decoding(self):
        cases = []
        missing = deepcopy(METADATA)
        del missing["packets"][30]
        cases.append(missing)
        for field, value in (("pts", 29), ("dts", 29), ("duration", 0), ("stream_index", 1), ("flags", "__C")):
            metadata = deepcopy(METADATA)
            metadata["packets"][30][field] = value
            cases.append(metadata)
        unknown_last_duration = deepcopy(METADATA)
        del unknown_last_duration["packets"][-1]["duration"]
        cases.append(unknown_last_duration)
        for metadata in cases:
            runner = FakeRunner(metadata)
            with self.subTest(metadata=metadata["packets"][30]):
                with self.assertRaises(ArtworkError) as failure:
                    self.download(runner)
                self.assertEqual(failure.exception.code, "VIDEO_TIMELINE_INVALID")
                self.assertEqual(len(runner.calls), 1)
                self.assertEqual(list(self.folder.iterdir()), [])

    def test_presentation_gap_and_overlap_cannot_cancel_in_total_duration(self):
        metadata = deepcopy(METADATA)
        metadata["streams"][0]["time_base"] = "1/60"
        cursor = 0
        for index, packet in enumerate(metadata["packets"]):
            duration = 3 if index == 1 else (1 if index == 3 else 2)
            packet.update(pts=index * 2, dts=cursor, duration=duration)
            cursor += duration
        self.assertEqual(sum(packet["duration"] for packet in metadata["packets"]), 120)
        self.assertEqual(max(packet["pts"] + packet["duration"] for packet in metadata["packets"]), 120)
        with self.assertRaises(ArtworkError) as failure:
            self.download(FakeRunner(metadata))
        self.assertEqual(failure.exception.code, "VIDEO_TIMELINE_INVALID")
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_packet_span_must_still_match_hls_without_relaxing_tolerance(self):
        longer = replace(INSPECTION, layout=replace(LAYOUT, duration=3))
        with self.assertRaises(ArtworkError) as failure:
            self.download(inspection=longer)
        self.assertEqual(failure.exception.code, "VIDEO_DURATION_MISMATCH")

    def test_decode_endpoint_count_and_no_dup_drop_are_required(self):
        for progress in (decode_progress(microseconds=1_000_000), decode_progress(frames=59),
                         decode_progress(dup=1), decode_progress(drop=1), b"frame=60\nprogress=end\n"):
            with self.subTest(progress=progress):
                with self.assertRaises(ArtworkError) as failure:
                    self.download(FakeRunner(progress=progress))
                self.assertEqual(failure.exception.code, "VIDEO_DECODE_TIMELINE_MISMATCH")
                self.assertEqual(list(self.folder.iterdir()), [])

    def test_truncated_transfer_cleans_its_part_before_any_probe(self):
        runner = FakeRunner()
        client = FakeMediaClient(ArtworkError("DOWNLOAD_LENGTH_MISMATCH", "Truncated synthetic media.", "download"))
        with self.assertRaises(OutputCommitError) as failure:
            self.download(runner, client)
        self.assertFalse(failure.exception.committed)
        self.assertEqual(runner.calls, [])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_existing_destination_avoids_transfer_and_never_overwrites(self):
        artifact = self.download()
        client = FakeMediaClient()
        with self.assertRaises(ArtworkError) as failure:
            self.download(client=client)
        self.assertEqual(failure.exception.code, "OUTPUT_CONFLICT")
        self.assertEqual(client.calls, 0)
        self.assertEqual((self.folder / artifact["path"]).read_bytes(), BODY)

    def test_postcommit_cleanup_failure_keeps_validated_artifact_and_residue(self):
        tools = MediaTools("/synthetic/ffmpeg", "/synthetic/ffprobe", "synthetic", "synthetic", FakeRunner())
        with OutputDirectory(self.folder) as output:
            with patch("apple_music_artwork.download.os.unlink", side_effect=PermissionError("synthetic cleanup")):
                with self.assertRaises(OutputCommitError) as failure:
                    download_motion(ASSET, INSPECTION, output, FakeMediaClient(), tools)
        error = failure.exception
        self.assertTrue(error.committed)
        self.assertEqual(error.code, "OUTPUT_CLEANUP_FAILED")
        self.assertEqual(error.artifact["status"], "downloaded")
        self.assertTrue(error.artifact["validation"]["full_decode"])
        self.assertEqual((self.folder / error.path).read_bytes(), BODY)
        self.assertEqual(len(error.residue), 1)
        self.assertEqual((self.folder / error.residue[0]).read_bytes(), BODY)
        self.assertFalse(Path(error.residue[0]).is_absolute())

    def test_prepare_writes_and_validates_once_but_only_coordinator_can_publish(self):
        control = RuntimeControl(monotonic=lambda: 0)
        inside = []
        class Gate:
            def __enter__(self):
                stages = [event["stage"] for event in control.drain_events()]
                self_test.assertEqual(stages, ["download", "waiting_validation"])
                inside.append(True)
            def __exit__(self, *args):
                inside.pop()
        class Runner(FakeRunner):
            def run(self, argv, **limits):
                if not inside:
                    raise AssertionError("Media tools must run inside the validation gate")
                return super().run(argv, **limits)
        class Client(FakeMediaClient):
            attempt = 2
            item_id = "video"
            def stream_to(self, url, sink, **limits):
                if inside:
                    raise AssertionError("Network transfer must not occupy the media tool gate")
                self.limits = limits
                return super().stream_to(url, sink, **limits)
        self_test = self
        client, runner = Client(), Runner()
        tools = MediaTools("/synthetic/ffmpeg", "/synthetic/ffprobe", "synthetic", "synthetic", runner)
        with OutputDirectory(self.folder) as output:
            temporary = output.allocate_temp(motion_filename(ASSET, INSPECTION))
            artifact = prepare_motion(ASSET, INSPECTION, temporary, client, tools,
                                      control=control, deadline=300, validation_gate=Gate())
            self.assertEqual(client.calls, 1)
            self.assertEqual(client.limits["max_attempts"], 1)
            self.assertEqual(client.limits["deadline"], 300)
            self.assertTrue(temporary.record()["sealed"])
            self.assertFalse((self.folder / artifact["path"]).exists())
            self.assertEqual(temporary.path.read_bytes(), BODY)
            self.assertTrue(artifact["validation"]["full_decode"])
            self.assertNotIn(str(self.folder), json.dumps(artifact))
            events = control.drain_events()
            self.assertEqual([event["attempt"] for event in events], [2])
            self.assertEqual([event["stage"] for event in events], ["validate"])
            output.publish_temp(temporary)
            output.cleanup_temp(temporary)
            self.assertEqual((self.folder / artifact["path"]).read_bytes(), BODY)

    def test_prepare_failure_retains_registered_temp_for_coordinator_cleanup(self):
        tools = MediaTools("/synthetic/ffmpeg", "/synthetic/ffprobe", "synthetic", "synthetic", FakeRunner(decode_code=1))
        with OutputDirectory(self.folder) as output:
            temporary = output.allocate_temp(motion_filename(ASSET, INSPECTION))
            with self.assertRaises(ArtworkError) as failure:
                prepare_motion(ASSET, INSPECTION, temporary, FakeMediaClient(), tools)
            self.assertEqual(failure.exception.code, "VIDEO_DECODE_FAILED")
            self.assertEqual(list(self.folder.iterdir()), [temporary.path])
            output.cleanup_temp(temporary)

    def test_cancelled_prepare_performs_no_transfer_or_media_process(self):
        control = RuntimeControl()
        control.cancel()
        runner, client = FakeRunner(), FakeMediaClient()
        tools = MediaTools("/synthetic/ffmpeg", "/synthetic/ffprobe", "synthetic", "synthetic", runner)
        with OutputDirectory(self.folder) as output:
            temporary = output.allocate_temp(motion_filename(ASSET, INSPECTION))
            with self.assertRaises(ArtworkError) as failure:
                prepare_motion(ASSET, INSPECTION, temporary, client, tools, control=control)
            self.assertEqual(failure.exception.code, "CANCELLED")
            self.assertEqual((client.calls, runner.calls), (0, []))
            self.assertEqual(temporary.path.stat().st_size, 0)
            output.cleanup_temp(temporary)

    def test_decode_events_parse_split_lines_and_use_verified_packet_duration(self):
        control = RuntimeControl()
        class Runner(FakeRunner):
            def run(self, argv, **limits):
                result = super().run(argv, **limits)
                callback = limits.get("on_stdout")
                if callback:
                    callback(b"out_ti")
                    callback(b"me_us=500000\n")
                    callback(b"out_time_us=2000000\nprogress=end\n")
                return result
        metadata = deepcopy(METADATA)
        metadata["format"]["duration"] = "12.0"
        runner = Runner(metadata)
        tools = MediaTools("/synthetic/ffmpeg", "/synthetic/ffprobe", "synthetic", "synthetic", runner)
        with OutputDirectory(self.folder) as output:
            temporary = output.allocate_temp(motion_filename(ASSET, INSPECTION))
            artifact = prepare_motion(ASSET, INSPECTION, temporary, FakeMediaClient(), tools, control=control, item_id="video")
            events = [event for event in control.drain_events() if event["kind"] == "decode"]
            self.assertEqual(events[-1]["processed_seconds"], 2)
            self.assertEqual(events[-1]["total_seconds"], 2)
            self.assertEqual(artifact["validation"]["timing_declarations"]["container"]["duration"], "12.0")
            output.cleanup_temp(temporary)


class MediaToolTests(TestCase):
    def test_missing_tools_and_bad_config_are_explicit_without_running(self):
        runner = FakeRunner()
        with patch("apple_music_artwork.motion.shutil.which", return_value=None):
            with self.assertRaises(ArtworkError) as failure:
                MediaTools.discover(runner=runner)
        self.assertEqual(failure.exception.code, "MEDIA_TOOL_MISSING")
        with self.assertRaises(ArtworkError) as failure:
            MediaTools.discover(ffmpeg="relative/ffmpeg", runner=runner)
        self.assertEqual(failure.exception.code, "MEDIA_TOOL_UNAVAILABLE")
        self.assertEqual(runner.calls, [])

    def test_explicit_tools_are_checked_and_manifest_omits_local_paths(self):
        runner = FakeRunner()
        tools = MediaTools.discover(ffmpeg=sys.executable, ffprobe=sys.executable, runner=runner)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(tools.ffmpeg_version, "synthetic-1")
        self.assertNotIn(sys.executable, json.dumps(tools.to_dict()))
        self.assertNotIn(sys.executable, repr(tools))

    @skipUnless(os.name == "posix", "Pipe selector runner is used by POSIX downloads")
    def test_real_runner_bounds_stdout_stderr_timeout_and_reports_exit(self):
        runner = ProcessRunner()
        result = runner.run([sys.executable, "-B", "-c", "import sys; sys.stdout.write('out'); sys.stderr.write('err'); sys.exit(3)"], timeout=5, max_output_bytes=100)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, b"out", b"err"))
        for script, timeout, code in (("import sys; sys.stdout.write('a'*100000)", 5, "MEDIA_TOOL_OUTPUT_LIMIT"),
                                       ("import time; time.sleep(5)", 0.05, "MEDIA_TOOL_TIMEOUT")):
            with self.subTest(code=code), self.assertRaises(ArtworkError) as failure:
                runner.run([sys.executable, "-B", "-c", script], timeout=timeout, max_output_bytes=100)
            self.assertEqual(failure.exception.code, code)

    @skipUnless(os.name == "posix", "Pipe selector runner is used by POSIX downloads")
    def test_real_runner_emits_before_exit_and_cancel_kills_and_reaps_owned_child(self):
        control, first_output = RuntimeControl(), Event()
        received, processes = [], []
        start = subprocess.Popen
        def record_process(*args, **kwargs):
            process = start(*args, **kwargs)
            processes.append(process)
            return process
        def progress(chunk):
            received.append(chunk)
            first_output.set()
        script = "import sys,time; sys.stdout.write('out_time_us=500000\\n'); sys.stdout.flush(); time.sleep(30)"
        with patch("apple_music_artwork.motion.subprocess.Popen", side_effect=record_process):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(ProcessRunner().run, [sys.executable, "-B", "-c", script],
                                     timeout=5, max_output_bytes=1000, control=control, on_stdout=progress)
                try:
                    self.assertTrue(first_output.wait(2))
                    self.assertFalse(future.done())
                finally:
                    control.cancel()
                with self.assertRaises(ArtworkError) as failure:
                    future.result(timeout=2)
        self.assertEqual(failure.exception.code, "CANCELLED")
        self.assertEqual(b"".join(received), b"out_time_us=500000\n")
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed and processes[0].stderr.closed)
