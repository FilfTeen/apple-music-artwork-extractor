"""Terminal behavior is deterministic and never supplies execution truth."""

from copy import deepcopy
from io import StringIO
import json
import os
import signal
from threading import Event, Thread, get_ident
from unittest import TestCase
from unittest.mock import patch

from apple_music_artwork.cli import main
from apple_music_artwork.client import HttpClient, HttpResponse
from apple_music_artwork.errors import ArtworkError
from apple_music_artwork.presentation import ProgressReporter, render_result
from apple_music_artwork.runtime import RuntimeControl


URL = "https://music.apple.com/us/album/example/1"


class Terminal(StringIO):
    def isatty(self):
        return True


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ProgressTests(TestCase):
    def test_auto_only_emits_to_tty_and_never_emits_nowhere(self):
        for stream, mode, expected in ((StringIO(), "auto", False), (Terminal(), "auto", True),
                                       (StringIO(), "always", True), (Terminal(), "never", False)):
            with self.subTest(mode=mode, tty=stream.isatty()):
                reporter = ProgressReporter(stream, mode)
                reporter.emit({"kind": "stage", "stage": "metadata"})
                reporter.close()
                self.assertEqual(bool(stream.getvalue()), expected)

    def test_tty_refreshes_at_most_four_times_each_second(self):
        stream, clock = Terminal(), Clock()
        reporter = ProgressReporter(stream, clock=clock, sleep=clock.sleep)
        for timestamp in (0, .01, .24, .25, .49, .50, .74, .75, .99):
            clock.now = timestamp
            reporter.emit({"kind": "transfer", "received_bytes": int(timestamp * 100)})
        reporter.close()
        reporter.close()
        self.assertEqual(stream.getvalue().count("\r"), 5)
        self.assertGreaterEqual(clock.now, 1)
        self.assertEqual(stream.getvalue().count("\n"), 1)

    def test_redirected_progress_is_rate_limited_plain_text(self):
        stream, clock = StringIO(), Clock()
        reporter = ProgressReporter(stream, "always", clock=clock)
        for timestamp in (0, .1, .5, .99, 1, 1.99, 2):
            clock.now = timestamp
            reporter.emit({"kind": "transfer", "received_bytes": int(timestamp * 100),
                           "semantic_path": "artwork\x1b[31m\nline"})
        reporter.close()
        self.assertEqual(len(stream.getvalue().splitlines()), 3)
        self.assertNotIn("\x1b", stream.getvalue())
        self.assertNotIn("\r", stream.getvalue())

    def test_unknown_total_has_no_percentage_or_eta(self):
        stream = StringIO()
        reporter = ProgressReporter(stream, "always")
        reporter.emit({"kind": "transfer", "received_bytes": 1234})
        self.assertIn("1.2 KiB", stream.getvalue())
        self.assertNotIn("%", stream.getvalue())
        self.assertNotIn("ETA", stream.getvalue())

    def test_retry_attempt_starts_from_zero_without_reusing_old_progress(self):
        stream, clock = StringIO(), Clock()
        reporter = ProgressReporter(stream, "always", clock=clock)
        reporter.emit({"kind": "transfer", "received_bytes": 50, "expected_bytes": 100, "attempt": 1})
        clock.now = 1
        reporter.emit({"kind": "retry", "attempt": 2, "retry_after_seconds": 1})
        clock.now = 2
        reporter.emit({"kind": "transfer", "received_bytes": 0, "expected_bytes": 100, "attempt": 2})
        lines = stream.getvalue().splitlines()
        self.assertIn("50.0%", lines[0])
        self.assertIn("整文件从头下载", lines[1])
        self.assertIn("0 B / 100 B（0.0%）", lines[2])
        self.assertIn("第 2 次尝试", lines[2])

    def test_full_transfer_is_followed_by_validation_and_save(self):
        stream, clock = StringIO(), Clock()
        reporter = ProgressReporter(stream, "always", clock=clock)
        events = ({"kind": "transfer", "received_bytes": 100, "expected_bytes": 100},
                  {"kind": "stage", "stage": "validate"},
                  {"kind": "decode", "processed_seconds": 1, "total_seconds": 2},
                  {"kind": "stage", "stage": "save"})
        for number, event in enumerate(events):
            clock.now = number
            reporter.emit(event)
        lines = stream.getvalue().splitlines()
        self.assertIn("100.0%", lines[0])
        self.assertEqual(lines[1], "验证")
        self.assertIn("完整解码 1 秒 / 2 秒（50.0%）", lines[2])
        self.assertEqual(lines[3], "保存")
        self.assertNotIn("成功", stream.getvalue())

    def test_file_ordinal_and_semantic_label_are_not_reference_count(self):
        stream = StringIO()
        reporter = ProgressReporter(stream, "always")
        reporter.emit({"kind": "stage", "stage": "queued", "ordinal": 2, "total": 3,
                       "semantic_path": "editorialVideo.motionDetailSquare", "active": 2, "queued": 1})
        self.assertIn("条目 2/3 · 排队等待", stream.getvalue())
        self.assertIn("方形动态封面", stream.getvalue())
        self.assertIn("运行 2，排队 1", stream.getvalue())

    def test_stages_in_one_refresh_window_are_preserved_until_final_flush(self):
        for stream, interval in ((StringIO(), 1), (Terminal(), .25)):
            with self.subTest(tty=stream.isatty()):
                clock = Clock()
                reporter = ProgressReporter(stream, "always", clock=clock, sleep=clock.sleep, columns=500)
                reporter.emit({"kind": "transfer", "received_bytes": 100, "expected_bytes": 100})
                clock.now = interval / 10
                reporter.emit({"kind": "retry", "attempt": 2})
                reporter.emit({"kind": "transfer", "received_bytes": 0, "expected_bytes": 100, "attempt": 2})
                reporter.emit({"kind": "stage", "stage": "validate"})
                reporter.emit({"kind": "stage", "stage": "save"})
                self.assertNotIn("保存", stream.getvalue())
                reporter.close()
                self.assertEqual(clock.now, interval)
                self.assertIn("第 2 次尝试", stream.getvalue())
                self.assertIn("验证", stream.getvalue())
                self.assertIn("保存", stream.getvalue())
                self.assertEqual(stream.getvalue().count("保存"), 1)

    def test_coordinator_tick_displays_pending_stage_without_another_event(self):
        stream, clock = StringIO(), Clock()
        reporter = ProgressReporter(stream, "always", clock=clock, sleep=clock.sleep)
        reporter.emit({"kind": "transfer", "received_bytes": 100, "expected_bytes": 100})
        clock.now = .1
        reporter.emit({"kind": "stage", "stage": "validate"})
        clock.now = 1
        reporter.flush()
        self.assertEqual(stream.getvalue().splitlines()[-1], "验证")

    def test_narrow_tty_keeps_current_progress_before_long_boundary_history(self):
        stream, clock = Terminal(), Clock()
        reporter = ProgressReporter(stream, clock=clock, sleep=clock.sleep, columns=80)
        label = "editorialVideo.motionDetailSquare." + "long-name" * 20
        reporter.emit({"kind": "stage", "stage": "download", "semantic_path": label})
        clock.now = .01
        for event in ({"kind": "retry", "attempt": 2}, {"kind": "stage", "stage": "validate"},
                      {"kind": "decode", "processed_seconds": 4, "total_seconds": 10}):
            reporter.emit(dict(event, semantic_path=label))
        clock.now = .25
        reporter.flush()
        current = stream.getvalue().split("\r")[-1]
        self.assertIn("完整解码 4 秒 / 10 秒（40.0%）", current)


class CliPresentationTests(TestCase):
    def invoke(self, args, executor=None, stderr=None):
        stdout, stderr = StringIO(), StringIO() if stderr is None else stderr
        with patch("apple_music_artwork.cli._execute", side_effect=executor) as execute:
            code = main(args, stdout=stdout, stderr=stderr, environ={})
        return code, stdout.getvalue(), stderr.getvalue(), execute

    def test_jobs_defaults_to_two_and_explicit_list_jobs_are_rejected(self):
        def execute(args, **kwargs):
            self.assertEqual(args.jobs, 2)
            self.assertFalse(args.jobs_explicit)
            return {"status": "success", "errors": [], "downloads": []}, 0
        code, stdout, stderr, called = self.invoke([URL, "--json"], execute)
        self.assertEqual(code, 0)
        self.assertEqual(called.call_count, 1)
        self.assertEqual(stderr, "")
        for jobs in ("1", "2", "4"):
            code, stdout, stderr, called = self.invoke([URL, "--list", "--jobs", jobs, "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(stdout)["errors"][0]["code"], "INVALID_ARGUMENTS")
            called.assert_not_called()

    def test_jobs_accepts_only_one_to_four(self):
        for value in ("0", "5", "-1", "1.5", "private-secret"):
            code, stdout, stderr, called = self.invoke([URL, "--jobs", value, "--json"])
            self.assertEqual(code, 2)
            self.assertNotIn("private-secret", stdout + stderr)
            called.assert_not_called()

    def test_help_is_chinese_and_keeps_machine_argument_names(self):
        code, stdout, stderr, called = self.invoke(["--help"])
        self.assertEqual(code, 0)
        for text in ("用法：", "选项：".rstrip("："), "显示帮助并退出", "--jobs", "--progress", "原目录自动恢复旧快照"):
            self.assertIn(text, stdout)
        self.assertNotIn("show this help message", stdout)
        self.assertEqual(stderr, "")
        called.assert_not_called()

    def test_progress_stays_in_stderr_and_json_is_one_unchanged_document(self):
        expected = {"schema_version": 2, "status": "success", "downloads": [], "errors": []}
        def execute(args, *, event_sink, **kwargs):
            event_sink.emit({"kind": "stage", "stage": "metadata"})
            return expected, 0
        code, stdout, stderr, _ = self.invoke([URL, "--progress", "always", "--json"], execute)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), expected)
        self.assertEqual(stderr, "获取素材信息\n")
        self.assertNotIn("\x1b", stdout + stderr)

    def test_successfully_parsed_json_abbreviation_uses_json_output(self):
        expected = {"schema_version": 2, "status": "success", "downloads": [], "errors": []}
        code, stdout, stderr, _ = self.invoke([URL, "--js"], lambda *args, **kwargs: (expected, 0))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), expected)
        self.assertEqual(stderr, "")

    def test_cancel_before_execution_finishes_emits_one_result_and_exit_130(self):
        def execute(args, *, event_sink, **kwargs):
            event_sink.emit({"kind": "stage", "stage": "metadata"})
            raise KeyboardInterrupt
        code, stdout, stderr, _ = self.invoke([URL, "--json"], execute, Terminal())
        self.assertEqual(code, 130)
        result = json.loads(stdout)
        self.assertEqual(result["errors"][0]["code"], "CANCELLED")
        self.assertTrue(result["run"]["cancelled"])
        self.assertTrue(stderr.endswith("\n"))

    def test_repeated_sigint_during_final_progress_wait_keeps_one_json(self):
        clock, received = Clock(), []
        previous = signal.getsignal(signal.SIGINT)
        def interrupted_wait(seconds):
            received.append(seconds)
            os.kill(os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGINT)
            clock.sleep(seconds)
        def reporter(stream, mode):
            return ProgressReporter(stream, mode, clock=clock, sleep=interrupted_wait)
        def execute(args, *, event_sink, **kwargs):
            event_sink.emit({"kind": "stage", "stage": "download"})
            clock.now = .1
            event_sink.emit({"kind": "stage", "stage": "cancelled"})
            return {"schema_version": 2, "status": "failed", "downloads": [],
                    "errors": [], "run": {"cancelled": True}}, 130
        with patch("apple_music_artwork.cli.ProgressReporter", side_effect=reporter):
            code, stdout, stderr, _ = self.invoke([URL, "--json", "--progress", "always"], execute)
        self.assertEqual(code, 130)
        self.assertTrue(json.loads(stdout)["run"]["cancelled"])
        self.assertEqual(received, [.9])
        self.assertEqual(stderr.splitlines(), ["下载", "取消"])
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_never_hides_progress_but_keeps_chinese_error_and_next_step(self):
        def execute(args, *, event_sink, **kwargs):
            event_sink.emit({"kind": "stage", "stage": "metadata"})
            raise ArtworkError("OUTPUT_BUSY", "private-secret", "download")
        code, stdout, stderr, _ = self.invoke([URL, "--progress", "never"], execute, Terminal())
        self.assertEqual(code, 6)
        self.assertNotIn("获取素材信息", stderr)
        self.assertIn("[OUTPUT_BUSY] 此输出目录正由另一任务使用", stderr)
        self.assertIn("不要删除锁文件", stderr)
        self.assertNotIn("private-secret", stdout + stderr)


class ResultRenderingTests(TestCase):
    def render(self, result):
        stdout, stderr = StringIO(), StringIO()
        original = deepcopy(result)
        render_result(result, json_output=False, stdout=stdout, stderr=stderr, output_dir="export")
        self.assertEqual(result, original)
        return stdout.getvalue(), stderr.getvalue()

    def test_reuse_counts_old_snapshot_and_incomplete_resource_are_explicit(self):
        result = {"status": "success", "downloads": [{"status": "downloaded", "action": "reused", "path": "one.png",
                  "validation": {"actual_dimensions": {"width": 64, "height": 64}, "actual_format": "PNG", "decoded": True}}],
                  "run": {"resumed": True, "snapshot": {"created_at": None}, "counts": {"total": 1, "new_downloaded": 0,
                           "reused": 1, "failed": 0, "cancelled": 0, "not_started": 0, "semantic_references": 3},
                          "stats": {"requests": 0, "received_bytes": 0}}, "errors": [],
                  "coverage": {"completeness": {"status": "incomplete", "unresolved": ["unknown_candidates"]}},
                  "manifest_report": {"path": "manifest.json", "status": "saved"}}
        stdout, stderr = self.render(result)
        for text in ("新下载 0，复用 1", "共 1 个文件、3 个语义引用", "保留原解码验证；本次进行本地核验",
                     "采集时间：未知", "获取最新素材请使用新目录", "本次实际请求 0 次", "本次命令：成功", "仍有未决项", "存在未知候选", "export/manifest.json"):
            self.assertIn(text, stdout)
        self.assertEqual(stderr, "")

    def test_motion_listing_explains_declared_specs_are_not_decode_evidence(self):
        result = {"mode": "list", "status": "success", "assets": [], "downloads": [], "errors": [],
                  "motion_inspections": [{"asset_id": "synthetic", "semantic_path": "editorialVideo.motionDetailSquare",
                    "status": "supported", "inspection": {"selected": {"width": 2160, "height": 2160,
                      "codec": "hevc", "frame_rate": 30}, "layout": {"duration": 15, "content_length": 1024}}}]}
        stdout, _ = self.render(result)
        for text in ("2160×2160 hevc", "30 fps", "15 秒", "1.0 KiB", "声明规格，尚未完整解码"):
            self.assertIn(text, stdout)

    def test_resumed_pending_inspection_discloses_new_checks_with_old_catalog(self):
        result = {"status": "partial", "downloads": [], "errors": [], "run": {"resumed": True,
                  "snapshot": {"created_at": "2026-10-08T00:00:00Z", "remote_freshness": "not_rechecked"},
                  "pending_inspection": {"attempted": 2, "completed": 1, "checked_at": "2026-10-09T00:00:00Z"}}}
        stdout, _ = self.render(result)
        for text in ("继续原资源快照", "补查上次未完成的动态规格，完成 1/2 项", "检查时间：2026-10-09T00:00:00Z", "资源目录信息未刷新"):
            self.assertIn(text, stdout)
        self.assertNotIn("未刷新远端资源", stdout)

    def test_specific_asset_error_and_long_cooldown_include_actionable_time(self):
        error = {"code": "RETRY_AFTER_DEADLINE", "message": "private-secret", "category": "download",
                 "retry_after_seconds": 600, "retry_at": 1000}
        result = {"status": "failed", "errors": [error], "downloads": [
                  {"status": "failed", "semantic_path": "editorialVideo.motionDetailSquare", "error": error}]}
        stdout, stderr = self.render(result)
        for text in ("素材 editorialVideo.motionDetailSquare", "服务要求等待 600 秒", "1970-01-01T00:16:40+00:00", "原参数和目录重跑"):
            self.assertIn(text, stderr)
        self.assertEqual(stderr.count("[RETRY_AFTER_DEADLINE]"), 1)
        self.assertNotIn("private-secret", stdout + stderr)

    def test_retained_residue_is_visible_and_never_becomes_a_success_claim(self):
        result = {"status": "partial", "downloads": [], "errors": [],
                  "run": {"residue": [".apple-artwork-state/unknown.part"]}}
        stdout, _ = self.render(result)
        self.assertIn("保留残留（未自动清理）：.apple-artwork-state/unknown.part", stdout)
        self.assertIn("本次命令：部分成功", stdout)

    def test_manifest_cleanup_failure_does_not_claim_the_committed_file_is_missing(self):
        result = {"status": "partial", "downloads": [], "errors": [],
                  "manifest_report": {"path": "manifest.json", "status": "failed", "committed": True}}
        stdout, _ = self.render(result)
        self.assertIn("清单：已提交，但收尾失败", stdout)

    def test_first_legacy_reuse_reports_the_actual_new_local_decode(self):
        result = {"status": "success", "errors": [], "downloads": [{"status": "downloaded", "action": "reused",
                  "path": "owned.mp4", "validation": {"actual_dimensions": {"width": 64, "height": 64},
                  "actual_format": "MP4"}, "local_verification": {"decoded_this_run": True}}]}
        stdout, _ = self.render(result)
        self.assertIn("本次完整本地解码验证通过", stdout)


class CliRuntimeProgressTests(TestCase):
    def test_metadata_cooldown_is_visible_while_waiting_and_only_coordinator_writes(self):
        clock, waiting, release, visible, retry_visible = Clock(), Event(), Event(), Event(), Event()
        writes, responses, result = [], [], {}
        payload = {"data": [{"id": "1", "type": "albums", "attributes": {"artwork": {
                   "url": "https://is1-ssl.mzstatic.com/image/thumb/owned/{w}x{h}bb.jpg", "width": 4, "height": 4}}}]}

        class ObservedStream(StringIO):
            def write(self, text):
                writes.append(get_ident())
                if "限流等待" in text:
                    visible.set()
                if "准备第 2 次尝试" in text:
                    retry_visible.set()
                return super().write(text)

        class Transport:
            def request(self, url, **kwargs):
                responses.append(url)
                if len(responses) == 1:
                    return HttpResponse(429, {"Retry-After": "2"}, b"")
                return HttpResponse(200, {}, json.dumps(payload).encode())

        def cooldown(seconds):
            clock.now += seconds
            waiting.set()
            if not release.wait(3):
                raise AssertionError("test did not release the controlled cooldown")

        control = RuntimeControl(monotonic=clock, sleep=cooldown)
        http, stdout, stderr = HttpClient(Transport(), control=control), StringIO(), ObservedStream()

        def run_cli():
            result["writer"] = get_ident()
            result["code"] = main([URL, "--provider", "catalog", "--list", "--json", "--progress", "always"],
                                  http=http, stdout=stdout, stderr=stderr,
                                  environ={"APPLE_MUSIC_DEVELOPER_TOKEN": "synthetic-test-value"})

        def reporter(stream, mode):
            return ProgressReporter(stream, mode, clock=clock, sleep=clock.sleep)

        worker = Thread(target=run_cli)
        with patch("apple_music_artwork.cli.ProgressReporter", side_effect=reporter):
            worker.start()
            try:
                self.assertTrue(waiting.wait(2), "the metadata retry never entered its cooldown")
                self.assertTrue(retry_visible.wait(2), "the retry was not reported during the controlled wait")
                clock.now += 1  # One redirected-progress interval, without ending the network wait.
                self.assertTrue(visible.wait(2), "cooldown feedback appeared only after the wait ended")
                self.assertEqual(len(responses), 1, "retry happened before the controlled wait was released")
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result["code"], 0, stdout.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["run"]["stats"]["requests"], 2)
        self.assertEqual(set(writes), {result["writer"]})
