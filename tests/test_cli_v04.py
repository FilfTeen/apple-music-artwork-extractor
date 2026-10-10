"""Human input and machine protocols stay separate at the actual CLI seam."""

from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import shlex
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch
import unicodedata

from apple_music_artwork.cli import main
from apple_music_artwork.interaction import TerminalInteraction
from apple_music_artwork.presentation import ProgressReporter, render_result


URL = "https://music.apple.com/us/album/example/1"


class Terminal(StringIO):
    def isatty(self):
        return True


class CliBoundaryTests(TestCase):
    def run_cli(self, args, *, stdin=None, stdout=None, stderr=None, execute=None):
        stdin = StringIO() if stdin is None else stdin
        stdout = StringIO() if stdout is None else stdout
        stderr = StringIO() if stderr is None else stderr
        result = {"schema_version": 2, "status": "success", "errors": [], "downloads": []}
        with patch("apple_music_artwork.cli._execute", side_effect=execute or (lambda *a, **k: (result, 0))) as called:
            code = main(args, stdin=stdin, stdout=stdout, stderr=stderr, environ={})
        return code, stdout.getvalue(), stderr.getvalue(), called

    def test_empty_noninteractive_command_returns_usage_without_work(self):
        code, out, err, called = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("plan URL", out)
        self.assertEqual(err, "")
        called.assert_not_called()

    def test_new_command_routing_and_defaults(self):
        cases = [("plan", [URL]), ("doctor", []), ("download", [URL]),
                 ("tasks.list", []), ("tasks.show", ["task-ref"]),
                 ("resume", ["task-ref"]), ("diagnostics", ["--task-dir", "owned"])]
        for command, tail in cases:
            with self.subTest(command=command):
                def execute(args, **kwargs):
                    self.assertEqual(args.command, command)
                    self.assertIsNone(args.library)
                    self.assertFalse(args.new_version)
                    self.assertEqual(args.jobs, 2)
                    return {"schema_version": 1, "kind": command, "status": "success", "data": {}, "errors": [], "warnings": []}, 0
                code, out, err, called = self.run_cli([*command.split("."), *tail, "--json"], execute=execute)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)["kind"], command)
                self.assertEqual(err, "")
                self.assertEqual(called.call_count, 1)

    def test_plan_admission_receives_explicit_default_values_and_canonical_destinations(self):
        def execute(args, **kwargs):
            self.assertIn("motion_profile", args.explicit_options)
            self.assertIn("provider", args.explicit_options)
            self.assertIn("plan_file", args.explicit_options)
            self.assertNotIn("url", args.explicit_options)
            self.assertEqual(args.motion_profile, "best")
            return {"schema_version": 2, "status": "success", "errors": [], "downloads": []}, 0
        code, _, _, _ = self.run_cli(["download", "--plan", "plan.json", "--motion-profile=best", "--provider", "web", "--json"], execute=execute)
        self.assertEqual(code, 0)

    def test_legacy_abbreviated_option_is_marked_explicit(self):
        def execute(args, **kwargs):
            self.assertIn("motion_profile", args.explicit_options)
            self.assertIn("output", args.explicit_options)
            self.assertIn("url", args.explicit_options)
            return {"schema_version": 2, "status": "success", "errors": [], "downloads": []}, 0
        code, _, _, _ = self.run_cli([URL, "--motion-prof", "best", "-oowned", "--json"], execute=execute)
        self.assertEqual(code, 0)

    def test_legacy_json_abbreviation_survives_parser_error(self):
        for args in ([URL, "--width", "private-value", "--js"],
                     ["--jso", URL, "--jobs", "private-value"],
                     [URL, "--width", "private-value", "--js=private-value"],
                     [URL, "--width", "private-value", "--json=private-value"],
                     [URL, "--events-jsonl=private-value"]):
            code, out, err, called = self.run_cli(args)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_ARGUMENTS")
            self.assertNotIn("private-value", out + err)
            self.assertEqual(err, "")
            called.assert_not_called()

    def test_human_argument_errors_keep_specific_correction_without_echo(self):
        for args, expected in ((["download", URL, "--jobs", "private-value"], "1、2、3、4 中的一个整数"),
                               (["download", URL, "-o", "one", "--library", "two"], "不能同时使用"),
                               (["download"], "请提供 Apple Music 公开链接")):
            code, out, err, called = self.run_cli(args)
            self.assertEqual(code, 2)
            self.assertIn(expected, err)
            self.assertNotIn("private-value", out + err)
            called.assert_not_called()

    def test_new_commands_reject_abbreviations_with_json_error_document(self):
        code, out, err, called = self.run_cli(["plan", URL, "--js"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["schema_version"], 1)
        self.assertEqual(err, "")
        called.assert_not_called()

    def test_exact_output_and_library_are_mutually_exclusive(self):
        code, out, err, called = self.run_cli(["download", URL, "-o", "one", "--library", "two", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["status"], "failed")
        called.assert_not_called()

    def test_query_commands_reject_inapplicable_download_options(self):
        for args in (["tasks", "list", "--all-media"], ["capabilities", "--new-version"],
                     ["diagnostics", "--task-dir", "owned", "--width", "10"],
                     ["resume", "task-ref", "--motion-profile", "compatible"]):
            code, out, err, called = self.run_cli([*args, "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_ARGUMENTS")
            self.assertEqual(err, "")
            called.assert_not_called()

    def test_no_interaction_if_any_standard_stream_is_redirected(self):
        for streams in ((StringIO(), Terminal(), Terminal()), (Terminal(), StringIO(), Terminal()),
                        (Terminal(), Terminal(), StringIO())):
            with self.subTest(streams=tuple(s.isatty() for s in streams)):
                def execute(args, *, decision_provider, **kwargs):
                    self.assertIsNone(decision_provider)
                    return {"schema_version": 2, "status": "success", "downloads": [], "errors": []}, 0
                self.run_cli([URL], stdin=streams[0], stdout=streams[1], stderr=streams[2], execute=execute)

    def test_explicit_machine_modes_disable_interaction_even_on_terminal(self):
        for mode in ("--json", "--no-input", "--events-jsonl"):
            def execute(args, *, decision_provider, **kwargs):
                self.assertIsNone(decision_provider)
                return {"schema_version": 2, "status": "success", "downloads": [], "errors": []}, 0
            code, out, err, _ = self.run_cli([URL, mode], stdin=Terminal(), stdout=Terminal(), stderr=Terminal(), execute=execute)
            self.assertEqual(code, 0)
            if mode != "--no-input":
                self.assertEqual(json.loads(out)["status"], "success")
                self.assertEqual(err, "")

    def test_only_real_interactive_mode_receives_callback(self):
        def execute(args, *, decision_provider, **kwargs):
            self.assertTrue(callable(decision_provider))
            return {"schema_version": 2, "status": "success", "downloads": [], "errors": []}, 0
        self.run_cli([URL], stdin=Terminal(), stdout=Terminal(), stderr=Terminal(), execute=execute)

    def test_events_are_separate_redacted_json_lines_and_result_is_one_document(self):
        def execute(args, *, event_sink, **kwargs):
            for sequence in (1, 2):
                event_sink.emit({"kind": "transfer", "schema_version": 1, "sequence": sequence,
                                 "received_bytes": sequence, "raw_response": "private-value"})
            return {"schema_version": 2, "status": "success", "downloads": [], "errors": []}, 0
        code, out, err, _ = self.run_cli([URL, "--events-jsonl", "--progress", "always"], execute=execute)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "success")
        self.assertEqual([json.loads(line)["sequence"] for line in err.splitlines()], [1, 2])
        self.assertNotIn("private-value", out + err)
        self.assertNotIn("\x1b", out + err)

    def test_failed_event_channel_is_visible_without_interrupting_task_result(self):
        class BrokenEvents(StringIO):
            def write(self, text):
                raise BrokenPipeError("private-value")
        def execute(args, *, event_sink, **kwargs):
            event_sink.emit({"kind": "stage", "stage": "save"})
            return {"schema_version": 2, "status": "success", "downloads": [], "errors": []}, 0
        code, out, _, _ = self.run_cli([URL, "--events-jsonl"], stderr=BrokenEvents(), execute=execute)
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["event_report"]["code"], "EVENT_OUTPUT_FAILED")
        self.assertNotIn("private-value", out)

    def test_capabilities_arguments_match_real_command_surface_and_guide_is_self_contained(self):
        out, err = StringIO(), StringIO()
        code = main(["capabilities", "--guide", "--json"], stdout=out, stderr=err, environ={})
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["data"]["protocol"]["query_schema_version"], 1)
        tasks = result["data"]["arguments"]["tasks list"]
        flags = [flag for item in tasks for flag in item["flags"]]
        self.assertIn("--library", flags)
        self.assertNotIn("--all-media", flags)
        guide = result["data"]["guide"]
        self.assertTrue(guide.startswith("---\nname: apple-artwork-operator\n"))
        self.assertIn("apple-artwork download --plan", guide)
        self.assertNotIn("references/", guide)
        self.assertEqual(err.getvalue(), "")


class InstalledProtocolFlowTests(TestCase):
    """Exercise parser, workflow, persistence, and machine streams together."""

    def setUp(self):
        from test_execution import response
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "response.json"
        self.source.write_text(json.dumps(response(2)), encoding="utf-8")
        self.task = self.root / "task"

    def invoke(self, arguments, transport=None, *, events=False):
        from apple_music_artwork.client import HttpClient
        from test_execution import NoNetwork
        out, err = StringIO(), StringIO()
        code = main([*arguments, "--events-jsonl" if events else "--json", "--no-input"],
                    stdin=StringIO(), stdout=out, stderr=err, environ={},
                    http=HttpClient(transport or NoNetwork()))
        result = json.loads(out.getvalue())
        self.assertNotIn("\x1b", out.getvalue() + err.getvalue())
        if not events:
            self.assertEqual(err.getvalue(), "")
        return code, result, [json.loads(line) for line in err.getvalue().splitlines()]

    def test_capabilities_plan_download_show_resume_and_events(self):
        from test_execution import Images
        code, capabilities, _ = self.invoke(["capabilities", "--guide"])
        self.assertEqual(code, 0)
        self.assertIn("apple-artwork download --plan", capabilities["data"]["guide"])
        code, planned, _ = self.invoke(["plan", URL, "--response", str(self.source),
                                        "--all-static", "-o", str(self.task)])
        self.assertEqual(code, 0, planned)
        self.assertFalse(self.task.exists())
        saved = self.root / "plan.json"
        saved.write_text(json.dumps(planned), encoding="utf-8")
        self.source.rename(self.root / "source-moved.json")
        transport = Images()
        code, downloaded, events = self.invoke(["download", "--plan", str(saved)], transport, events=True)
        self.assertEqual(code, 0, downloaded)
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(events)
        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(len({event["run_id"] for event in events}), 1)
        code, shown, _ = self.invoke(["tasks", "show", "--task-dir", str(self.task)])
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["data"]["control"]["lifecycle"], "completed")
        code, resumed, _ = self.invoke(["resume", "--task-dir", str(self.task)])
        self.assertEqual(code, 0, resumed)
        self.assertEqual(resumed["run"]["counts"]["reused"], 2)

    def test_machine_pending_defer_and_retry_preserve_successful_file(self):
        from test_execution import Images
        code, pending, _ = self.invoke(["download", URL, "--response", str(self.source),
                                       "--all-static", "-o", str(self.task)], Images(bad={"synthetic-1"}))
        self.assertEqual(code, 8, pending)
        choices = self.root / "choices.json"
        def choose(context, action):
            choices.write_text(json.dumps({"protocol_version": 1, "choices": [{
                "plan_id": context["plan_id"], "decision_id": context["decision_id"],
                "action": action, "params": {}}]}), encoding="utf-8")
        choose(pending["control"]["decisions"][0], "defer")
        code, deferred, _ = self.invoke(["resume", "--task-dir", str(self.task), "--decisions", str(choices)])
        self.assertEqual(code, 9, deferred)
        self.assertEqual(deferred["manifest_report"]["status"], "saved")
        code, shown, _ = self.invoke(["tasks", "show", "--task-dir", str(self.task)])
        self.assertEqual(code, 0, shown)
        choose(shown["data"]["control"]["decisions"][0], "retry")
        transport = Images()
        code, resumed, _ = self.invoke(["resume", "--task-dir", str(self.task), "--decisions", str(choices)], transport)
        self.assertEqual(code, 0, resumed)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(resumed["run"]["counts"]["reused"], 1)
        code, stale, _ = self.invoke(["resume", "--task-dir", str(self.task), "--decisions", str(choices)])
        self.assertEqual(code, 2, stale)
        self.assertEqual(stale["errors"][0]["code"], "DECISION_STALE")

    def test_explicit_skip_is_visible_in_real_result_without_claiming_completion(self):
        from test_execution import Images
        code, pending, _ = self.invoke(["download", URL, "--response", str(self.source),
                                       "--all-static", "-o", str(self.task)], Images(bad={"synthetic-1"}))
        self.assertEqual(code, 8, pending)
        context = pending["control"]["decisions"][0]
        choices = self.root / "skip.json"
        choices.write_text(json.dumps({"protocol_version": 1, "choices": [{"plan_id": context["plan_id"],
                           "decision_id": context["decision_id"], "action": "skip", "params": {}}]}), encoding="utf-8")
        code, skipped, _ = self.invoke(["resume", "--task-dir", str(self.task), "--decisions", str(choices)])
        self.assertEqual(code, 0, skipped)
        out, err = StringIO(), StringIO()
        render_result(skipped, json_output=False, stdout=out, stderr=err, output_dir=str(self.task))
        self.assertIn("复用 1", out.getvalue())
        self.assertIn("已跳过 1", out.getvalue())
        self.assertIn("已跳过 方形编辑封面 · 图片（未完成，不计为已保存）", out.getvalue())
        self.assertEqual(skipped["coverage"]["completeness"]["status"], "incomplete")


class InteractionTests(TestCase):
    def context(self):
        return {"plan_id": "plan-current", "decision_id": "decision-current", "reason": "网络暂时不可用",
                "options": [{"action": "retry", "label": "再次尝试", "params": {"jobs": 1}},
                            {"action": "defer", "label": "稍后处理", "params": {}}]}

    def interaction(self, value, *, wait=None):
        return TerminalInteraction(Terminal(value), Terminal(), timeout=120, clock=lambda: 0,
                                   wait=wait or (lambda readers, w, e, seconds: (readers, [], [])))

    def test_selected_action_is_bound_to_plan_and_decision(self):
        context = self.context()
        original = deepcopy(context)
        choice = self.interaction("1\n").decide(context)
        self.assertEqual(choice, {"plan_id": "plan-current", "decision_id": "decision-current",
                                  "action": "retry", "params": {"jobs": 1}})
        self.assertEqual(context, original)

    def test_menu_explains_reason_and_asset_specs_without_using_display_as_choice(self):
        context = self.context()
        context["reason"] = "NETWORK_ERROR"
        context["display"] = {"items": [{"name": "视频 2", "name_key": "editorialVideo.motionDetailSquare.video",
                                           "kind": "video", "spec": {"width": 2160, "height": 2160, "codec": "hevc"}}]}
        interaction = self.interaction("2\n")
        response = interaction.decide(context)
        text = interaction.stderr.getvalue()
        self.assertIn("网络传输失败", text)
        self.assertIn("方形动态封面", text)
        self.assertIn("2160×2160 hevc", text)
        self.assertNotIn("editorialVideo", text)
        self.assertEqual(set(response), {"plan_id", "decision_id", "action", "params"})

    def test_retry_time_is_shown_from_context_without_changing_choice(self):
        context = self.context()
        context["retry_at"] = 1893456000
        interaction = self.interaction("1\n")
        choice = interaction.decide(context)
        self.assertIn("2030-01-01T00:00:00+00:00（UTC）", interaction.stderr.getvalue())
        self.assertNotIn("retry_at", choice)

    def test_eof_and_timeout_have_no_implied_choice(self):
        context = self.context()
        self.assertIsNone(self.interaction("").decide(context))
        waits = []
        def wait(r, w, e, seconds):
            waits.append(seconds)
            return [], [], []
        self.assertIsNone(self.interaction("2\n", wait=wait).decide(context))
        self.assertEqual(waits, [120])

    def test_defer_requires_explicit_choice(self):
        for value in ("2\n", "q\n", "later\n"):
            self.assertEqual(self.interaction(value).decide(self.context())["action"], "defer")

    def test_invalid_input_is_not_evaluated_or_echoed(self):
        interaction = self.interaction("private-value\n0\n999\n2\n")
        self.assertEqual(interaction.decide(self.context())["action"], "defer")
        self.assertNotIn("private-value", interaction.stderr.getvalue())

    def test_keyboard_interrupt_stays_cancellation(self):
        interaction = self.interaction("", wait=Mock(side_effect=KeyboardInterrupt))
        with self.assertRaises(KeyboardInterrupt):
            interaction.decide(self.context())

    def test_wizard_returns_only_selected_operation(self):
        self.assertEqual(self.interaction("3\n").wizard(), ["resume"])
        self.assertEqual(self.interaction("2\n" + URL + "\n3\n").wizard(), ["download", URL, "--all-media"])
        self.assertIsNone(self.interaction("q\n").wizard())


class HumanResumeTests(TestCase):
    def test_skipped_unresolved_source_is_not_counted_as_a_file(self):
        result = {"schema_version": 2, "status": "success", "errors": [],
                  "downloads": [{"status": "skipped", "kind": "video", "semantic_path": "editorialVideo.motionDetailSquare"}],
                  "run": {"counts": {"total": 0, "skipped_issues": 1, "semantic_references": 1}},
                  "control": {"lifecycle": "completed", "decision_required": False},
                  "coverage": {"completeness": {"status": "incomplete", "unresolved": ["explicitly_skipped_assets"]}}}
        out, err = StringIO(), StringIO()
        render_result(result, json_output=False, stdout=out, stderr=err)
        self.assertIn("已跳过 0；共 0 个文件", out.getvalue())
        self.assertIn("另有 1 个尚未形成文件计划的素材来源已明确跳过", out.getvalue())
        self.assertIn("存在用户明确跳过的素材", out.getvalue())

    def test_result_uses_semantic_names_and_one_authoritative_resume_step(self):
        error = {"code": "NETWORK_ERROR", "category": "download"}
        result = {"schema_version": 2, "status": "partial", "errors": [error], "downloads": [
                  {"status": "downloaded", "kind": "image", "semantic_path": "artwork", "path": "images/hash-token.png",
                   "validation": {"actual_dimensions": {"width": 16, "height": 16}, "actual_format": "PNG"}},
                  {"status": "failed", "kind": "video", "semantic_path": "editorialVideo.motionDetailSquare", "error": error}],
                  "control": {"decision_required": True, "lifecycle": "needs_action", "resume": {}},
                  "manifest_report": {"path": "manifest.json", "status": "saved"}}
        out, err = StringIO(), StringIO()
        render_result(result, json_output=False, stdout=out, stderr=err, output_dir="owned task")
        self.assertIn("已保存 主封面 · 图片 · 实际 16×16 PNG", out.getvalue())
        self.assertIn("输出位置：owned task", out.getvalue())
        self.assertNotIn("hash-token", out.getvalue())
        self.assertIn("素材 方形动态封面", err.getvalue())
        self.assertNotIn("原参数和目录重跑", err.getvalue())
        self.assertEqual((out.getvalue() + err.getvalue()).count("下一步"), 1)

    def test_narrow_terminal_keeps_quoted_exact_resume_path_copyable(self):
        task = "owned export/很长的中文任务名称 owner's edition"
        result = {"schema_version": 2, "status": "partial", "downloads": [], "errors": [],
                  "control": {"decision_required": True, "lifecycle": "deferred", "resume": {"requires_task_dir": True}},
                  "manifest_report": {"path": "manifest.json", "status": "saved"}}
        out, err = Terminal(), Terminal()
        with patch("apple_music_artwork.presentation._columns", return_value=40):
            render_result(result, json_output=False, stdout=out, stderr=err, destination={"path": task, "mode": "exact"})
        command, = [line for line in out.getvalue().splitlines() if line.startswith("apple-artwork ")]
        self.assertEqual(shlex.split(command), ["apple-artwork", "resume", "--task-dir", task])
        self.assertEqual(out.getvalue().count("下一步"), 1)

    def test_task_query_retains_custom_library_and_nested_error_retry_time(self):
        reference = "a" * 64 + "@2"
        result = {"schema_version": 1, "kind": "tasks.show", "status": "success",
                  "data": {"control": {"lifecycle": "needs_action", "resume": {"task_ref": reference}}},
                  "errors": [{"code": "NETWORK_ERROR", "context": {"retry_at": 1893456000}}]}
        out, err = Terminal(), Terminal()
        with patch("apple_music_artwork.presentation._columns", return_value=40):
            render_result(result, json_output=False, stdout=out, stderr=err, library="owned custom library")
        command, = [line for line in out.getvalue().splitlines() if line.startswith("apple-artwork ")]
        self.assertEqual(shlex.split(command), ["apple-artwork", "resume", reference, "--library", "owned custom library"])
        self.assertIn("2030-01-01T00:00:00+00:00", err.getvalue())


class QueueDisplayTests(TestCase):
    def snapshot(self):
        return {"kind": "snapshot", "counts": {"completed": 0, "reused": 2, "failed": 1, "pending": 3},
                "active": 4, "queued": 3, "needs_action": 1,
                "items": [{"item_id": str(i), "name": "中文长名称" * 12, "kind": "image", "stage": "waiting_validation",
                           "received_bytes": 100, "total_bytes": 100} for i in range(5)]}

    def test_snapshot_is_complete_fact_and_transfer_is_not_saved_count(self):
        stream = StringIO()
        reporter = ProgressReporter(stream, "always", columns=80)
        reporter.emit(self.snapshot())
        reporter.close()
        out = stream.getvalue()
        self.assertIn("已保存 0", out)
        self.assertIn("已复用 2", out)
        self.assertIn("待启动 3", out)
        self.assertIn("待处理 1", out)
        self.assertEqual(out.count("等待验证"), 4)
        self.assertEqual(out.count("100.0%"), 4)

    def test_plain_terminal_has_no_escapes_and_respects_chinese_columns(self):
        for width in (40, 80, 120):
            stream = Terminal()
            reporter = ProgressReporter(stream, "always", columns=width)
            reporter.configure(plain=True, no_color=True)
            reporter.emit(self.snapshot())
            reporter.close()
            self.assertNotIn("\x1b", stream.getvalue())
            for line in stream.getvalue().splitlines():
                cells = sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1 for c in line)
                self.assertLess(cells, width)

    def test_resize_is_checked_on_each_snapshot_frame(self):
        stream = Terminal()
        ticks = iter((0, 1, 2, 3))
        reporter = ProgressReporter(stream, "always", clock=lambda: next(ticks))
        reporter.configure(plain=True)
        with patch("apple_music_artwork.presentation._columns", side_effect=[120, 40]) as columns:
            reporter.emit(self.snapshot())
            reporter.emit(self.snapshot())
        self.assertEqual(columns.call_count, 2)
