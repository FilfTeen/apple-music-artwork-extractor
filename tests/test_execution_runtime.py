"""CLI coordination with synthetic transports; fake video probes are not media proof.

Image bytes are generated and decoded locally. Motion bodies only exercise file
coordination. The cancellation case uses a real, owned Python child to test
process cleanup independently from the synthetic media-tool responses.
"""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from threading import Condition, Event, Lock, current_thread, main_thread
from unittest import TestCase, skipUnless
from urllib.parse import urlsplit

from PIL import Image

from apple_music_artwork.cli import main
from apple_music_artwork.client import HttpClient, HttpResponse
from apple_music_artwork.motion import MediaTools, ProcessResult, ProcessRunner
from apple_music_artwork.runtime import RuntimeControl


URL = "https://music.apple.com/us/album/example/1?l=en-US"
BODY = b"synthetic-body-for-coordination-tests-only"
MASTER = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000,AVERAGE-BANDWIDTH=900,"
          'CODECS="avc1.640028",RESOLUTION=4x4,FRAME-RATE=2\nmedia.m3u8\n').encode()
MEDIA = ("#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:1\n"
         '#EXT-X-MAP:URI="movie.mp4",BYTERANGE="4@0"\n'
         f"#EXTINF:1,\n#EXT-X-BYTERANGE:{len(BODY) - 4}@4\nmovie.mp4\n"
         "#EXT-X-ENDLIST\n").encode()
PROGRESS = b"frame=2\nout_time_us=1000000\ndup_frames=0\ndrop_frames=0\nprogress=end\n"


def media_paths(output, pattern):
    return [path for directory in (output, output / "images", output / "videos")
            if directory.is_dir() for path in directory.glob(pattern)]


def payload(images=0, videos=0):
    attributes = {"editorialArtwork": {}, "editorialVideo": {}}
    image_fields = ("artwork", "emailFeature", "staticDetailSquare", "staticDetailTall",
                    "storeFlowcase", "subscriptionHero")
    video_fields = ("motionDetailSquare", "motionDetailTall", "motionSquareVideo1x1",
                    "motionTallVideo3x4")
    for number, field in enumerate(image_fields[:images]):
        value = {"url": f"https://a.mzstatic.com/synthetic/image-{number}/{{w}}x{{h}}bb.{{f}}",
                 "width": 4, "height": 4}
        if field == "artwork":
            attributes[field] = value
        else:
            attributes["editorialArtwork"][field] = value
    for number, field in enumerate(video_fields[:videos]):
        attributes["editorialVideo"][field] = {
            "video": f"https://mvod.itunes.apple.com/synthetic/video-{number}/master.m3u8"}
    return {"data": [{"id": "1", "type": "albums", "href": "/v1/catalog/us/albums/1",
                      "attributes": attributes}]}


def png_bytes():
    stream = BytesIO()
    with Image.new("RGBA", (4, 4), (31, 62, 93, 200)) as image:
        image.save(stream, format="PNG")
    return stream.getvalue()


def probe_bytes():
    return json.dumps({
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "1",
                   "start_time": "0", "size": str(len(BODY))},
        "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "width": 4,
                     "height": 4, "avg_frame_rate": "2/1", "r_frame_rate": "2/1",
                     "duration": "1", "time_base": "1/2", "start_pts": 0,
                     "start_time": "0", "duration_ts": 2, "pix_fmt": "yuv420p"}],
        "packets": [{"stream_index": 0, "pts": index, "dts": index, "duration": 1,
                     "flags": "___"} for index in range(2)],
    }).encode()


class Clock:
    def __init__(self):
        self.lock, self.value, self.waits = Lock(), 0, []

    def now(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value += seconds

    def sleep(self, seconds):
        with self.lock:
            self.waits.append(seconds)
            self.value += seconds


class RecordingControl(RuntimeControl):
    """Observe the public control seam without replacing coordinator behavior."""

    def __init__(self, **options):
        super().__init__(**options)
        self.observation = Condition()
        self.observed = []
        self.hook = None
        self.fence_predicate = None
        self.fence_cycles = 0
        self.fence = Event()

    def emit(self, event):
        with self.observation:
            self.observed.append(dict(event))
            self.observation.notify_all()
        super().emit(event)
        if self.hook is not None:
            self.hook(dict(event))

    def drain_events(self):
        events = super().drain_events()
        if self.fence_predicate is not None and self.fence_predicate():
            self.fence_cycles += 1
            if self.fence_cycles >= 2:
                self.fence.set()
        return events

    def stage_items(self, stage):
        with self.observation:
            return {event["item_id"] for event in self.observed
                    if event.get("kind") == "stage" and event.get("stage") == stage
                    and event.get("item_id") is not None}


class Scenario:
    def __init__(self, output, *, block_images=False, image_ready=None,
                 reset_attempts=0, clock=None, partial_seconds=0):
        self.output, self.condition = output, Condition()
        self.image = png_bytes()
        self.release_images = Event()
        if not block_images:
            self.release_images.set()
        self.image_ready = image_ready
        self.reset_attempts, self.clock, self.partial_seconds = reset_attempts, clock, partial_seconds
        self.adapters, self.workers, self.requests, self.image_calls = [], set(), [], []
        self.image_active = self.image_peak = self.bytes_returned = 0
        self.streams, self.completed_streams, self.closed_streams = [], [], []

    def record_request(self, method, url, adapter, options):
        with self.condition:
            self.requests.append((method, url, adapter, options))
            self.condition.notify_all()

    def return_bytes(self, value):
        with self.condition:
            self.bytes_returned += len(value)
        return value

    def image_count(self):
        with self.condition:
            return len(self.image_calls)

    def motion_count(self):
        with self.condition:
            return len(self.completed_streams)


class Transport:
    """Unknown inputs fail closed; forks share observations, never adapters."""

    def __init__(self, scenario):
        self.scenario = scenario
        with scenario.condition:
            scenario.adapters.append(self)

    def fork(self):
        return Transport(self.scenario)

    def request(self, url, **options):
        scenario = self.scenario
        scenario.record_request("GET", url, self, options)
        parsed = urlsplit(url)
        if parsed.hostname == "a.mzstatic.com" and parsed.path.startswith("/synthetic/image-"):
            with scenario.condition:
                scenario.workers.add(current_thread())
                scenario.image_calls.append(self)
                scenario.image_active += 1
                scenario.image_peak = max(scenario.image_peak, scenario.image_active)
                scenario.condition.notify_all()
            try:
                if not scenario.release_images.wait(5):
                    raise AssertionError("Test did not release blocked image transfers")
                if scenario.image_ready is not None and not scenario.image_ready.wait(5):
                    raise AssertionError("Owned decoder did not reach the cancellation barrier")
                return HttpResponse(200, {"Content-Type": "image/png"}, scenario.return_bytes(scenario.image))
            finally:
                with scenario.condition:
                    scenario.image_active -= 1
                    scenario.condition.notify_all()
        if parsed.hostname != "mvod.itunes.apple.com" or not parsed.path.startswith("/synthetic/video-"):
            raise AssertionError("Unexpected synthetic transport URL")
        if parsed.path.endswith("/master.m3u8"):
            body = MASTER
        elif parsed.path.endswith("/media.m3u8"):
            body = MEDIA
        else:
            raise AssertionError("Unexpected synthetic metadata URL")
        return HttpResponse(200, {"Content-Type": "application/vnd.apple.mpegurl"}, scenario.return_bytes(body))

    def head(self, url, **options):
        assert url.startswith("https://mvod.itunes.apple.com/synthetic/video-") and url.endswith("/movie.mp4")
        self.scenario.record_request("HEAD", url, self, options)
        return HttpResponse(200, {"Content-Type": "video/mp4", "Content-Length": str(len(BODY))}, b"")

    def stream(self, url, **options):
        assert url.startswith("https://mvod.itunes.apple.com/synthetic/video-") and url.endswith("/movie.mp4")
        scenario = self.scenario
        scenario.record_request("STREAM", url, self, options)
        with scenario.condition:
            scenario.workers.add(current_thread())
            attempt = len(scenario.streams) + 1
            temporaries = tuple((path.name, path.stat().st_size)
                                for path in sorted(media_paths(scenario.output, ".apple-artwork-*.part")))
            scenario.streams.append({"attempt": attempt, "url": url, "timeout": options["timeout"],
                                     "temporaries": temporaries})
            scenario.condition.notify_all()
        return BodyResponse(scenario, attempt)


class BodyResponse:
    status = 200
    headers = {"Content-Type": "video/mp4", "Content-Length": str(len(BODY))}

    def __init__(self, scenario, attempt):
        self.scenario, self.attempt, self.offset = scenario, attempt, 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        with self.scenario.condition:
            self.scenario.closed_streams.append(self.attempt)
            self.scenario.condition.notify_all()

    def read(self, size):
        scenario = self.scenario
        if self.attempt <= scenario.reset_attempts and self.offset:
            raise ConnectionResetError("Synthetic connection reset after a partial body")
        if self.offset == len(BODY):
            with scenario.condition:
                scenario.completed_streams.append(self.attempt)
                scenario.condition.notify_all()
            return b""
        chunk = BODY[self.offset:self.offset + min(size, 8)]
        self.offset += len(chunk)
        if self.offset == 8 and self.attempt <= scenario.reset_attempts and scenario.clock is not None:
            scenario.clock.advance(scenario.partial_seconds)
        return scenario.return_bytes(chunk)


class Runner:
    def __init__(self, *, block=False, child=False):
        self.lock, self.active, self.peak = Lock(), 0, 0
        self.calls, self.child_pid = [], None
        self.probe_entered, self.decoder_ready, self.release = Event(), Event(), Event()
        self.child = child
        if not block:
            self.release.set()

    def run(self, argv, **limits):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append(tuple(argv))
        try:
            if "-show_entries" in argv:
                self.probe_entered.set()
                if not self.release.wait(5):
                    raise AssertionError("Test did not release the blocked media probe")
                return ProcessResult(0, probe_bytes(), b"")
            if self.child:
                pending = bytearray()

                def progress(chunk):
                    pending.extend(chunk)
                    while b"\n" in pending:
                        raw, _, remainder = pending.partition(b"\n")
                        pending[:] = remainder
                        if raw.startswith(b"owned_pid="):
                            self.child_pid = int(raw.split(b"=", 1)[1])
                        if raw == b"out_time_us=0":
                            self.decoder_ready.set()
                    callback = limits.get("on_stdout")
                    if callback is not None:
                        callback(chunk)

                script = ("import os,time\nprint('owned_pid=' + str(os.getpid()), flush=True)\n"
                          "print('out_time_us=0', flush=True)\ntime.sleep(30)\n")
                return ProcessRunner().run([sys.executable, "-B", "-c", script], timeout=5,
                                           max_output_bytes=1024, control=limits.get("control"),
                                           on_stdout=progress)
            callback = limits.get("on_stdout")
            if callback is not None:
                callback(PROGRESS)
            return ProcessResult(0, PROGRESS, b"")
        finally:
            with self.lock:
                self.active -= 1


class ExecutionCase:
    @classmethod
    def setUpClass(cls):
        runtime = Path(tempfile.gettempdir()).resolve()
        for ancestor in (runtime, *runtime.parents):
            assert not ancestor.is_symlink()
        runtime.mkdir(parents=True, exist_ok=True)
        cls.scope = Path(tempfile.mkdtemp(prefix="v03-media-exec-", dir=runtime))

    def case(self, *, images=0, videos=0):
        folder = Path(tempfile.mkdtemp(prefix="case-", dir=self.scope))
        response = folder / "response.json"
        response.write_text(json.dumps(payload(images, videos)), encoding="utf-8")
        return folder / "output", response

    def invoke(self, output, response, scenario, *, control=None, runner=None, jobs=None, mode="--all-media"):
        control = control if control is not None else RecordingControl()
        http = HttpClient(Transport(scenario), control=control)
        stdout, stderr = StringIO(), StringIO()
        options = [URL, "--response", str(response), "-o", str(output), mode,
                   "--format", "png", "--json", "--progress", "never"]
        if jobs is not None:
            options += ["--jobs", str(jobs)]
        tools = None if runner is None else MediaTools("/synthetic/ffmpeg", "/synthetic/ffprobe",
                                                       "synthetic", "synthetic", runner)
        code = main(options, http=http, media_tools=tools, stdout=stdout, stderr=stderr, environ={})
        self.assertEqual(stderr.getvalue(), "")
        return code, json.loads(stdout.getvalue())

    def assert_finished(self, output, scenario, result):
        self.assertTrue(scenario.workers)
        self.assertTrue(all(not thread.is_alive() for thread in scenario.workers))
        self.assertEqual(media_paths(output, ".apple-artwork-*.part"), [])
        self.assertEqual(result["run"]["stats"]["requests"], len(scenario.requests))
        self.assertEqual(result["run"]["stats"]["received_bytes"], scenario.bytes_returned)
        self.assertEqual(len(scenario.closed_streams), len(scenario.streams))
        saved = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["downloads"], result["downloads"])
        self.assertEqual(saved["run"], result["run"])


@skipUnless(os.name == "posix", "Safe file publication currently requires POSIX")
class ExecutionImageRuntimeTests(ExecutionCase, TestCase):
    def test_default_two_and_explicit_four_are_real_overlapping_file_limits(self):
        for jobs, expected in ((None, 2), (4, 4)):
            with self.subTest(jobs=jobs):
                output, response = self.case(images=6)
                scenario = Scenario(output, block_images=True)
                control = RecordingControl()
                control.fence_predicate = lambda: scenario.image_count() >= expected
                with ThreadPoolExecutor(max_workers=1) as driver:
                    future = driver.submit(self.invoke, output, response, scenario, control=control,
                                           jobs=jobs, mode="--all-static")
                    try:
                        self.assertTrue(control.fence.wait(5), "Coordinator did not fill the file slots")
                        with scenario.condition:
                            self.assertEqual(scenario.image_active, expected)
                            self.assertEqual(len(scenario.image_calls), expected)
                            self.assertEqual(len({id(adapter) for adapter in scenario.image_calls}), expected)
                    finally:
                        scenario.release_images.set()
                    code, result = future.result(timeout=5)
                self.assertEqual(code, 0, result.get("errors"))
                self.assertEqual(result["run"]["jobs"], expected)
                self.assertEqual(result["run"]["counts"]["new_downloaded"], 6)
                self.assertEqual(scenario.image_peak, expected)
                self.assertEqual(len({id(adapter) for adapter in scenario.image_calls}), 6)
                self.assert_finished(output, scenario, result)
                for item in result["downloads"]:
                    self.assertTrue(item["validation"]["decoded"])
                    self.assertEqual((output / item["path"]).read_bytes(), scenario.image)


@skipUnless(os.name == "posix", "Safe file publication currently requires POSIX")
class ExecutionMotionRuntimeTests(ExecutionCase, TestCase):
    def test_waiting_for_probe_or_decode_keeps_file_slots_and_tool_peak_one(self):
        output, response = self.case(videos=3)
        scenario, control, runner = Scenario(output), RecordingControl(), Runner(block=True)
        control.fence_predicate = lambda: scenario.motion_count() >= 2 and len(control.stage_items("validate") | control.stage_items("waiting_validation")) >= 2
        with ThreadPoolExecutor(max_workers=1) as driver:
            future = driver.submit(self.invoke, output, response, scenario, control=control,
                                   runner=runner, mode="--all-motion")
            try:
                self.assertTrue(runner.probe_entered.wait(5), "No video reached the media gate")
                self.assertTrue(control.fence.wait(5), "Both transfers did not reach validation")
                with scenario.condition:
                    self.assertEqual(len(scenario.streams), 2)
                    self.assertEqual(len(scenario.completed_streams), 2)
                self.assertEqual(len(media_paths(output, ".apple-artwork-*.part")), 2)
                self.assertEqual(media_paths(output, "*.mp4"), [])
                with runner.lock:
                    self.assertEqual(runner.active, 1)
                    self.assertEqual(len(runner.calls), 1)
            finally:
                runner.release.set()
            code, result = future.result(timeout=5)
        self.assertEqual(code, 0, result.get("errors"))
        self.assertEqual(result["run"]["counts"]["new_downloaded"], 3)
        self.assertEqual(len(scenario.streams), 3)
        self.assertEqual(len(runner.calls), 6)
        self.assertEqual(runner.peak, 1)
        self.assert_finished(output, scenario, result)

    def retry_case(self, resets, seconds):
        output, response = self.case(videos=1)
        clock = Clock()
        control = RecordingControl(monotonic=clock.now, clock=clock.now, sleep=clock.sleep)
        scenario = Scenario(output, reset_attempts=resets, clock=clock, partial_seconds=seconds)
        runner = Runner()
        code, result = self.invoke(output, response, scenario, control=control, runner=runner,
                                   mode="--all-motion")
        self.assert_finished(output, scenario, result)
        names = []
        for attempt in scenario.streams:
            self.assertEqual(len(attempt["temporaries"]), 1, "Previous attempt must be removed before retry")
            name, size = attempt["temporaries"][0]
            self.assertEqual(size, 0, "A retry must start in a fresh, empty file")
            names.append(name)
        self.assertEqual(len(set(names)), len(names))
        return code, result, output, scenario, control, runner, clock

    def test_partial_resets_retry_from_new_files_with_one_deadline_and_byte_accounting(self):
        code, result, output, scenario, control, runner, clock = self.retry_case(2, 140)
        self.assertEqual(code, 0, result.get("errors"))
        self.assertEqual(len(scenario.streams), 3)
        self.assertEqual([attempt["timeout"] for attempt in scenario.streams], [20, 20, 17])
        self.assertEqual(clock.waits, [1, 2])
        self.assertEqual(scenario.bytes_returned, len(MASTER) + len(MEDIA) + 16 + len(BODY))
        self.assertEqual(result["run"]["stats"]["requests"], 6)
        self.assertEqual({event["attempt"] for event in control.observed
                          if event.get("kind") == "transfer" and event.get("received_bytes") == 0}, {1, 2, 3})
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(result["run"]["counts"]["new_downloaded"], 1)
        item = result["downloads"][0]
        self.assertEqual((output / item["path"]).read_bytes(), BODY)
        self.assertEqual(item["validation"]["sha256"], sha256(BODY).hexdigest())

    def test_partial_resets_stop_after_three_attempts(self):
        code, result, output, scenario, _, runner, _ = self.retry_case(3, 1)
        self.assertEqual(code, 8)
        self.assertEqual(result["control"]["lifecycle"], "needs_action")
        self.assertEqual(len(scenario.streams), 3)
        self.assertEqual(scenario.bytes_returned, len(MASTER) + len(MEDIA) + 24)
        self.assertEqual(result["run"]["counts"]["failed"], 1)
        self.assertEqual(result["downloads"][0]["error"]["code"], "NETWORK_ERROR")
        self.assertEqual(runner.calls, [])
        self.assertEqual(media_paths(output, "*.mp4"), [])

    def test_shared_deadline_stops_a_second_partial_transfer_without_third_attempt(self):
        code, result, output, scenario, _, runner, clock = self.retry_case(2, 150)
        self.assertEqual(code, 8)
        self.assertEqual(result["control"]["lifecycle"], "needs_action")
        self.assertEqual(len(scenario.streams), 2)
        self.assertEqual(clock.now(), 301)
        self.assertEqual(scenario.bytes_returned, len(MASTER) + len(MEDIA) + 16)
        self.assertEqual(result["downloads"][0]["error"]["code"], "NETWORK_TIMEOUT")
        self.assertEqual(runner.calls, [])
        self.assertEqual(media_paths(output, "*.mp4"), [])

    def test_sigint_keeps_concurrent_commit_and_reaps_workers_and_owned_child(self):
        self.assertIs(current_thread(), main_thread(), "SIGINT integration requires the main thread")
        output, response = self.case(images=1, videos=2)
        runner, control = Runner(child=True), RecordingControl()
        scenario = Scenario(output, image_ready=runner.decoder_ready)
        signalled = Event()
        previous_handler = signal.getsignal(signal.SIGINT)

        def cancel_after_commit(event):
            if event.get("stage") == "committed" and not signalled.is_set():
                self.assertTrue(runner.decoder_ready.is_set())
                signalled.set()
                for _ in range(3):
                    os.kill(os.getpid(), signal.SIGINT)

        control.hook = cancel_after_commit
        code, result = self.invoke(output, response, scenario, control=control, runner=runner)
        self.assertTrue(signalled.is_set(), result.get("errors"))
        self.assertEqual(code, 130)
        self.assertIs(signal.getsignal(signal.SIGINT), previous_handler)
        self.assertEqual(result["run"]["counts"], {"total": 3, "new_downloaded": 1, "reused": 0,
                                                  "failed": 0, "cancelled": 1, "not_started": 1,
                                                  "semantic_references": 3})
        self.assertEqual(len(scenario.streams), 1)
        self.assertTrue(result["run"]["cancelled"])
        self.assertEqual(len(result["downloads"]), 1)
        self.assertEqual(result["downloads"][0]["kind"], "image")
        interrupted = result["run"]["interrupted"]
        self.assertEqual({item["status"] for item in interrupted}, {"cancelled", "not_started"})
        self.assertEqual(len({item["item_id"] for item in interrupted}), 2)
        self.assertEqual(len(media_paths(output, "*.png")), 1)
        self.assertEqual(media_paths(output, "*.mp4"), [])
        self.assertIsInstance(runner.child_pid, int)
        with self.assertRaises(ProcessLookupError):
            os.kill(runner.child_pid, 0)
        self.assertEqual(runner.active, 0)
        self.assert_finished(output, scenario, result)
