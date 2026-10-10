"""Task navigation uses real local tasks and rejects sockets independently."""

from argparse import Namespace
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from apple_music_artwork import cli
from apple_music_artwork.client import HttpClient, HttpResponse
from apple_music_artwork.download import OutputDirectory
from apple_music_artwork.interaction import TerminalInteraction
from apple_music_artwork.jsonio import dumps_json
from apple_music_artwork.presentation import render_result
from apple_music_artwork.protocol import envelope
from apple_music_artwork.run_state import RunState
from apple_music_artwork.task_queries import query_tasks
from apple_music_artwork.workflow import dispatch
from test_execution import Images, NoNetwork, response
from test_remote_check import MetadataTransport


class Terminal(StringIO):
    def __init__(self, value="", *, on_read=None):
        super().__init__(value)
        self.on_read = on_read

    def isatty(self):
        return True

    def readline(self, *args):
        if self.on_read is not None:
            callback, self.on_read = self.on_read, None
            callback()
        return super().readline(*args)


class CliNavigationTests(TestCase):
    def setUp(self):
        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("offline CLI test attempted a socket operation"))
            guard.start()
            self.addCleanup(guard.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "output"

    def create(self, resource_id="1", *, name="同名专辑", new_version=False, bad=False):
        body = response()
        body["data"][0].update(id=resource_id, href=f"/v1/catalog/us/albums/{resource_id}")
        body["data"][0]["attributes"]["name"] = name
        source = self.root / (resource_id + ".json")
        source.write_text(dumps_json(body), encoding="utf-8")
        result, code = dispatch(Namespace(command="download",
            url=f"https://music.apple.com/us/album/example/{resource_id}?l=en-US",
            response=str(source), library=str(self.library), all_static=True,
            new_version=new_version, json=True, no_input=True, progress="never"),
            http=HttpClient(Images(bad={"synthetic-0"} if bad else ())), environ={})
        self.assertEqual(code, 8 if bad else 0, result)
        reference = result["control"]["resume"]["task_ref"]
        data, _ = query_tasks(self.library)
        row = next(row for row in data["tasks"] if row["task_ref"] == reference)
        return reference, self.library / row["relative_path"]

    def tree(self, path=None):
        path = path or self.library
        return {str(item.relative_to(path)): item.read_bytes() if item.is_file() else "directory"
                for item in path.rglob("*")} if path.exists() else {}

    def invoke(self, arguments, *, input_value="", interactive=False, stdin=None,
               stdout=None, stderr=None, wait=None, clock=None):
        factory = Terminal if interactive else StringIO
        stdin = factory(input_value) if stdin is None else stdin
        stdout = factory() if stdout is None else stdout
        stderr = factory() if stderr is None else stderr
        def interaction(source, destination, **kwargs):
            return TerminalInteraction(source, destination,
                wait=wait or (lambda readers, w, e, seconds: (readers, [], [])),
                clock=clock or (lambda: 0), **kwargs)
        with patch("apple_music_artwork.cli.TerminalInteraction", side_effect=interaction):
            code = cli.main(arguments, stdin=stdin, stdout=stdout, stderr=stderr,
                            http=HttpClient(NoNetwork()), environ={})
        return code, stdout.getvalue(), stderr.getvalue()

    def test_task_list_filters_compose_and_latest_precedes_status_union(self):
        first, _ = self.create("123")
        latest, _ = self.create("123", new_version=True, bad=True)
        self.create("456", name="另外专辑")
        code, out, _ = self.invoke(["tasks", "list", "--library", str(self.library),
            "--query", "123", "--type", "album", "--status", "completed", "--latest", "--json"])
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["data"]["tasks"], [])
        code, out, _ = self.invoke(["tasks", "list", "--library", str(self.library),
            "--query", "123", "--type", "album", "--status", "completed",
            "--status", "needs_action", "--latest", "--json"])
        self.assertEqual(code, 0, out)
        self.assertEqual([row["task_ref"] for row in json.loads(out)["data"]["tasks"]], [latest])
        self.assertNotEqual(first, latest)

    def test_filters_are_scoped_to_task_list_and_bad_type_is_input_error(self):
        for arguments in (["resume", "ref", "--query", "x"], ["tasks", "show", "ref", "--latest"],
                          ["doctor", "--status", "completed"], ["capabilities", "--type", "album"],
                          ["tasks", "list", "--type", "invalid"]):
            with self.subTest(arguments=arguments):
                code, out, err = self.invoke([*arguments, "--json"])
                self.assertEqual(code, 2, out)
                self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_ARGUMENTS")
                self.assertEqual(err, "")

    def test_human_list_shows_readable_facts_and_full_version_reference(self):
        reference, _ = self.create(name="展示专辑")
        code, out, err = self.invoke(["tasks", "list", "--library", str(self.library)])
        self.assertEqual(code, 0, err)
        for value in ("展示专辑", "album", "us", "en-US", "all-static", reference, "快照"):
            self.assertIn(value, out)

    def test_unknown_time_and_bad_state_are_visible_without_a_selectable_number(self):
        reference, _ = self.create()
        data, _ = query_tasks(self.library)
        data = deepcopy(data)
        data["tasks"][0].update(snapshot_created_at=None, selectable=False,
            error={"code": "RUN_STATE_INVALID", "category": "download"})
        out, err = StringIO(), StringIO()
        render_result(envelope("tasks.list", data), json_output=False, stdout=out, stderr=err, output_dir=None)
        self.assertIn(reference, out.getvalue())
        self.assertIn("未知", out.getvalue())
        self.assertIn("不可选择", out.getvalue())
        self.assertIn("RUN_STATE_INVALID", out.getvalue() + err.getvalue())

    def test_shared_list_and_picker_distinguish_exact_selection_values(self):
        self.create()
        original, _ = query_tasks(self.library)
        rows = []
        for label, field, value in (("semantic", "semantic_path", "artwork"),
                ("semantic", "semantic_path", "editorialArtwork.staticDetailSquare"),
                ("asset", "asset_id", "synthetic-selected-asset-123")):
            row = deepcopy(original["tasks"][0])
            row.update(selection_label=label, selection={field: value})
            rows.append(row)
        data = {"tasks": rows, "errors": []}
        out, err = StringIO(), StringIO()
        render_result(envelope("tasks.list", data), json_output=False, stdout=out, stderr=err, output_dir=None)
        interaction = TerminalInteraction(Terminal("q\n"), Terminal(),
            wait=lambda readers, w, e, timeout: (readers, [], []))
        self.assertIsNone(interaction.pick_task(data))
        for value in ("semantic · artwork", "semantic · editorialArtwork.staticDetailSquare",
                      "asset · synthetic-selected-asset-123"):
            self.assertIn(value, out.getvalue())
            self.assertIn(value, interaction.stderr.getvalue())

    def test_allocation_errors_are_not_presented_as_a_clean_empty_library(self):
        _, path = self.create()
        (path / ".apple-artwork-state" / "allocation.json").write_text("{}", encoding="utf-8")
        before = self.tree()
        code, out, err = self.invoke(["tasks", "list", "--library", str(self.library)])
        self.assertEqual(code, 0, out + err)
        self.assertIn("LIBRARY_INVALID", out + err)
        self.assertNotIn("暂无已登记任务", out + err)
        self.assertEqual(before, self.tree())

    def test_picker_reports_allocation_errors_without_waiting_or_executing(self):
        _, path = self.create()
        (path / ".apple-artwork-state" / "allocation.json").write_text("{}", encoding="utf-8")
        before = self.tree()
        def unexpected_wait(*args):
            raise AssertionError("corrupt allocation should not offer input")
        code, out, err = self.invoke(["resume", "--library", str(self.library)],
            interactive=True, wait=unexpected_wait)
        self.assertEqual(code, 0, out + err)
        self.assertIn("LIBRARY_INVALID", err)
        self.assertNotIn("暂无已登记任务", err)
        self.assertNotIn("已复用", out)
        self.assertEqual(before, self.tree())

    def test_interactive_resume_uses_real_workflow_and_the_same_immutable_binding(self):
        reference, path = self.create()
        captures = {}
        def query(root, **options):
            data, bindings = query_tasks(root, **options)
            captures["binding"] = bindings[reference]
            return data, bindings
        execute = cli._execute
        def consume(args, **kwargs):
            captures["consumed"] = args._selection_binding
            return execute(args, **kwargs)
        with patch("apple_music_artwork.cli.query_tasks", side_effect=query, create=True), \
             patch("apple_music_artwork.cli._execute", side_effect=consume):
            code, out, err = self.invoke(["resume", "--library", str(self.library)], interactive=True, input_value="1\n")
        self.assertEqual(code, 0, out + err)
        self.assertIs(captures["binding"], captures["consumed"])
        self.assertIn(reference, err)
        self.assertIn("已复用", out)
        self.assertEqual(json.loads((path / "manifest.json").read_text())["downloads"][0]["action"], "reused")

    def test_selection_then_state_change_is_rejected_by_real_workflow_without_writes(self):
        _, path = self.create()
        swapped = []
        def change():
            state = path / ".apple-artwork-state" / "state.json"
            state.write_bytes(state.read_bytes() + b"\n")
            swapped.append(self.tree())
        code, out, err = self.invoke(["resume", "--library", str(self.library)], interactive=True,
                                    stdin=Terminal("1\n", on_read=change))
        self.assertEqual(len(swapped), 1, "CLI did not reach actual numbered selection")
        self.assertEqual(code, 2, out + err)
        self.assertIn("PLAN_STALE", out + err)
        self.assertEqual(swapped[0], self.tree())

    def test_cancel_blank_and_eof_do_not_execute_or_change_the_task(self):
        self.create()
        before = self.tree()
        for value in ("q\n", "\n", ""):
            with self.subTest(value=value):
                code, out, err = self.invoke(["resume", "--library", str(self.library)], interactive=True, input_value=value)
                self.assertEqual(code, 0, out + err)
                self.assertNotIn("已复用", out)
                self.assertEqual(before, self.tree())

    def test_empty_library_does_not_create_a_task_or_wait_for_input(self):
        def unexpected_wait(*args):
            raise AssertionError("empty library should not wait")
        code, out, err = self.invoke(["resume", "--library", str(self.library)], interactive=True, wait=unexpected_wait)
        self.assertEqual(code, 0, out + err)
        self.assertFalse(self.library.exists())
        self.assertIn("暂无", out + err)

    def test_invalid_choice_does_not_reset_the_two_minute_deadline(self):
        self.create()
        before = self.tree()
        tick, waits = [0], []
        def wait(readers, w, e, seconds):
            waits.append(seconds)
            tick[0] += 60
            return (readers, [], []) if len(waits) == 1 else ([], [], [])
        code, out, err = self.invoke(["resume", "--library", str(self.library)], interactive=True,
                                    input_value="private-value\n", wait=wait, clock=lambda: tick[0])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(waits, [120, 60])
        self.assertNotIn("private-value", out + err)
        self.assertEqual(before, self.tree())

    def test_bad_row_number_cannot_execute_a_task(self):
        _, path = self.create()
        (path / ".apple-artwork-state" / "state.json").write_text("{}", encoding="utf-8")
        before = self.tree()
        code, out, err = self.invoke(["resume", "--library", str(self.library)], interactive=True, input_value="1\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("不可选择", out + err)
        self.assertNotIn("已复用", out)
        self.assertEqual(before, self.tree())

    def test_bad_number_does_not_hide_a_healthy_same_name_task(self):
        healthy, _ = self.create("100")
        _, broken_path = self.create("200")
        (broken_path / ".apple-artwork-state" / "state.json").write_text("{}", encoding="utf-8")
        data, _ = query_tasks(self.library)
        good_number = next(str(i) for i, row in enumerate(data["tasks"], 1) if row["task_ref"] == healthy)
        bad_number = next(str(i) for i, row in enumerate(data["tasks"], 1) if not row["selectable"])
        broken_before = self.tree(broken_path)
        code, out, err = self.invoke(["resume", "--library", str(self.library)],
            interactive=True, input_value=bad_number + "\n" + good_number + "\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("不可选择", err)
        self.assertIn("请输入可选择的编号", err)
        self.assertIn("已复用", out)
        self.assertEqual(broken_before, self.tree(broken_path))

    def test_machine_modes_and_redirected_streams_require_an_explicit_task(self):
        for flag in ("--json", "--no-input", "--events-jsonl"):
            code, out, err = self.invoke(["resume", flag], interactive=True, input_value="1\n")
            self.assertEqual(code, 2, out + err)
        for redirected in range(3):
            streams = [Terminal("1\n"), Terminal(), Terminal()]
            streams[redirected] = StringIO("1\n" if redirected == 0 else "")
            code, out, err = self.invoke(["resume"], stdin=streams[0], stdout=streams[1], stderr=streams[2])
            self.assertEqual(code, 2, out + err)

    def test_wizard_three_enters_the_same_numbered_resume_flow(self):
        reference, _ = self.create()
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            code, out, err = self.invoke([], interactive=True, input_value="3\n1\n")
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0, out + err)
        self.assertIn(reference, err)
        self.assertIn("已复用", out)

    def test_legal_pending_task_is_selectable_and_recovers_with_real_workflow(self):
        _, path = self.create()
        class Interrupted(BaseException):
            pass
        with OutputDirectory(path, create=False) as output, RunState(output) as state:
            state.data["control"]["lifecycle"] = "deferred"
            with patch.object(RunState, "_remove_marker", side_effect=Interrupted):
                with self.assertRaises(Interrupted):
                    state.save()
        marker = path / ".apple-artwork-state" / "state-transition.json"
        self.assertTrue(marker.is_file())
        code, out, err = self.invoke(["resume", "--library", str(self.library)], interactive=True, input_value="1\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("需要恢复元数据", err)
        self.assertIn("已复用", out)
        self.assertFalse(marker.exists())

    def test_capabilities_follow_scoped_filters_without_exposing_private_binding(self):
        code, out, err = self.invoke(["capabilities", "--json"])
        self.assertEqual(code, 0, err)
        arguments = json.loads(out)["data"]["arguments"]
        flags = [flag for option in arguments["tasks list"] for flag in option["flags"]]
        for flag in ("--query", "--type", "--status", "--latest"):
            self.assertIn(flag, flags)
            self.assertNotIn(flag, [value for option in arguments["resume"] for value in option["flags"]])
        self.assertNotIn("_selection_binding", out)
        self.assertNotIn("selection_token", out)
        self.create()
        code, out, _ = self.invoke(["tasks", "list", "--library", str(self.library), "--json"])
        self.assertEqual(code, 0)
        self.assertNotIn("TaskBinding", out)
        self.assertNotIn("state_sha256", out)
        self.assertNotIn(str(self.root), out)


class ComparisonCliSupport:
    def setUp(self):
        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("offline comparison CLI attempted a socket"))
            guard.start()
            self.addCleanup(guard.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.library = self.root / "library"

    def create(self, body=None, *, new_version=False, offline=False, **selection):
        body = response() if body is None else body
        args = Namespace(command="download", url="https://music.apple.com/us/album/example/1?l=en-US",
            library=str(self.library), provider="catalog", new_version=new_version,
            no_input=True, json=True, progress="never", **selection)
        if offline:
            source = self.root / "source.json"
            source.write_text(dumps_json(body), encoding="utf-8")
            args.response = str(source)
        result, code = dispatch(args, http=HttpClient(MetadataTransport(body)),
                                environ={"APPLE_MUSIC_DEVELOPER_TOKEN": "synthetic"})
        self.assertEqual(code, 0, result)
        reference = result["control"]["resume"]["task_ref"]
        data, _ = query_tasks(self.library)
        row = next(row for row in data["tasks"] if row["task_ref"] == reference)
        return reference, self.library / row["relative_path"]

    def tree(self):
        return {str(p.relative_to(self.library)): p.read_bytes() if p.is_file() else "directory"
                for p in self.library.rglob("*")} if self.library.exists() else {}

    def invoke(self, arguments, *, transport=None, environ=None):
        before = self.tree()
        out, err = StringIO(), StringIO()
        with patch("apple_music_artwork.library.allocate", side_effect=AssertionError("query allocated")), \
             patch("apple_music_artwork.run_state.RunState.__enter__", side_effect=AssertionError("query wrote state")), \
             patch("fcntl.flock", side_effect=AssertionError("query locked")):
            code = cli.main(arguments, stdin=StringIO(), stdout=out, stderr=err,
                http=HttpClient(NoNetwork() if transport is None else transport),
                environ={"APPLE_MUSIC_DEVELOPER_TOKEN": "synthetic"} if environ is None else environ)
        self.assertEqual(before, self.tree())
        return code, out.getvalue(), err.getvalue()


class CliComparisonTests(ComparisonCliSupport, TestCase):
    def test_diff_uses_actual_saved_versions_and_preserves_readonly_tree(self):
        first, _ = self.create()
        body = response()
        body["data"][0]["attributes"]["artwork"]["url"] = "https://a.mzstatic.com/new/{w}x{h}.jpg"
        second, _ = self.create(body, new_version=True)
        for left, right, outcome in ((first, first, "no_observed_change"), (first, second, "observed_change")):
            code, out, err = self.invoke(["tasks", "diff", left, right, "--library", str(self.library), "--json"])
            self.assertEqual(code, 0, out + err)
            result = json.loads(out)
            self.assertEqual((result["schema_version"], result["kind"], result["status"]), (1, "tasks.diff", "success"))
            self.assertEqual(result["data"]["outcome"], outcome)
            self.assertEqual(result["data"]["left"]["task_ref"], left)
            self.assertEqual(result["data"]["right"]["task_ref"], right)
            self.assertEqual(result["data"]["network_scope"],
                             {"resource_metadata": False, "hls_and_head": False, "media_bodies": False})
            self.assertEqual(err, "")
        code, out, err = self.invoke(["tasks", "diff", first, second, "--library", str(self.library)])
        self.assertEqual(code, 0, out + err)
        for text in ("观察到变化", first, second, "地址变化", "不证明", "媒体字节", "--json"):
            self.assertIn(text, out)
        self.assertNotIn("媒体内容已更新", out)

    def test_diff_rejects_missing_explicit_version_identity_and_extra_inputs(self):
        reference, _ = self.create()
        for arguments in (["tasks", "diff", reference],
                ["tasks", "diff", reference.split("@")[0], reference],
                ["tasks", "diff", "f" * 64 + "@1", reference],
                ["tasks", "diff", reference, reference, "--task-dir", "unused"],
                ["tasks", "diff", reference, reference, "--token-env", "SYNTHETIC"]):
            with self.subTest(arguments=arguments):
                code, out, err = self.invoke([*arguments, "--library", str(self.library), "--json"])
                self.assertEqual(code, 2, out + err)
                self.assertEqual(json.loads(out)["kind"], "tasks.diff")
                self.assertEqual(err, "")

    def test_update_guarded_metadata_uses_original_scope_and_invocation_token_env(self):
        reference, _ = self.create()
        body = response(2)
        body["data"][0]["attributes"]["artwork"]["width"] = 8
        transport = MetadataTransport(body)
        code, out, err = self.invoke(["tasks", "check-updates", reference,
            "--library", str(self.library), "--token-env", "SYNTHETIC_KEY", "--json"],
            transport=transport, environ={"SYNTHETIC_KEY": "synthetic_invocation_secret"})
        self.assertEqual(code, 0, out + err)
        result = json.loads(out)
        self.assertEqual((result["kind"], result["status"]), ("tasks.check-updates", "success"))
        self.assertEqual(result["data"]["outcome"], "observed_change")
        self.assertTrue(any(c["field"] == "width" for c in result["data"]["changes"]))
        self.assertEqual(result["data"]["network_scope"],
                         {"resource_metadata": True, "hls_and_head": False, "media_bodies": False})
        self.assertNotIn("version", result["data"]["right"])
        self.assertNotIn("task_ref", result["data"]["right"])
        self.assertEqual(len(transport.requests), 1)
        self.assertIn("/us/albums/1", transport.requests[0][1])
        self.assertIn("l=en-US", transport.requests[0][1])
        self.assertNotIn("SYNTHETIC_KEY", out)
        self.assertNotIn("synthetic_invocation_secret", out)
        self.assertEqual(err, "")

    def test_update_exact_directory_and_conflicting_locator_are_readonly(self):
        reference, path = self.create()
        code, out, err = self.invoke(["tasks", "check-updates", "--task-dir", str(path), "--json"],
                                    transport=MetadataTransport(response()))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(out)["data"]["outcome"], "no_observed_change")
        code, out, err = self.invoke(["tasks", "check-updates", reference, "--task-dir", str(path),
            "--library", str(self.library), "--json"])
        self.assertEqual(code, 2, out + err)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_TASK_REFERENCE")

    def test_update_missing_task_and_overrides_are_input_errors(self):
        reference, _ = self.create()
        for flags in ([], ["--provider", "web"], ["--response", "unused.json"], ["--all-media"],
                ["--semantic", "artwork"], ["--motion-profile", "compatible"], ["--width", "1"],
                ["--query", "x"], ["--latest"], ["--new-version"]):
            with self.subTest(flags=flags):
                arguments = ["tasks", "check-updates"] + ([reference] if flags else [])
                code, out, err = self.invoke([*arguments, *flags, "--library", str(self.library), "--json"])
                self.assertEqual(code, 2, out + err)
                self.assertEqual(json.loads(out)["kind"], "tasks.check-updates")

    def test_update_api_failure_keeps_indeterminate_data_and_mapped_exit(self):
        reference, _ = self.create()
        for machine in (False, True):
            code, out, err = self.invoke(["tasks", "check-updates", reference, "--library", str(self.library)] +
                                        (["--json"] if machine else []), transport=MetadataTransport(response(), status=404))
            self.assertEqual(code, 4, out + err)
            if machine:
                result = json.loads(out)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["data"]["outcome"], "indeterminate")
                self.assertIsNone(result["data"]["right"]["snapshot_created_at"])
                self.assertFalse(any(c["field"] == "presence" and c["after"]["available"]
                                     for c in result["data"]["changes"]))
            else:
                for text in ("无法判断", "未知", "请求失败", "资源信息", "RESOURCE_NOT_FOUND"):
                    self.assertIn(text, out + err)

    def test_offline_source_has_structured_limitation_and_no_http(self):
        reference, _ = self.create(offline=True)
        code, out, err = self.invoke(["tasks", "check-updates", reference, "--library", str(self.library), "--json"])
        self.assertEqual(code, 2, out + err)
        result = json.loads(out)
        self.assertIn({"reason": "offline_source"}, result["data"]["limitations"])
        self.assertEqual(result["data"]["network_scope"],
                         {"resource_metadata": False, "hls_and_head": False, "media_bodies": False})

    def test_pure_selection_ambiguity_keeps_known_changes_and_success_exit(self):
        reference, _ = self.create(semantic="artwork")
        body = response()
        second = deepcopy(body["data"][0])
        second["attributes"]["artwork"]["url"] = "https://a.mzstatic.com/another/{w}x{h}.jpg"
        body["data"].append(second)
        body["data"][0]["attributes"]["artwork"]["bgColor"] = "abcdef"
        code, out, err = self.invoke(["tasks", "check-updates", reference, "--library", str(self.library)],
                                    transport=MetadataTransport(body))
        self.assertEqual(code, 0, out + err)
        for text in ("无法判断", "declarations", "选择", "已观察", "不证明"):
            self.assertIn(text, out)

    def test_unknown_scan_diff_does_not_turn_unavailable_presence_into_deletion(self):
        import test_task_queries
        first, path = self.create(all_static=True)
        body = response(2)
        body["data"][0]["attributes"]["artwork"]["url"] = "https://a.mzstatic.com/new/{w}x{h}.jpg"
        second, _ = self.create(body, new_version=True, all_static=True)
        def remove_scan(payload):
            del payload["snapshot"]["manifest"]["coverage"]["scan"]
        test_task_queries.TaskQueryTests.rewrite_state(self, path, remove_scan)
        code, out, err = self.invoke(["tasks", "diff", first, second, "--library", str(self.library)])
        self.assertEqual(code, 0, out + err)
        for text in ("无法判断", "未能观察", "地址变化", "扫描"):
            self.assertIn(text, out)

    def test_unknown_snapshot_reference_and_version_are_explicit_without_json_changes(self):
        data = {"task_id": "a" * 64, "outcome": "indeterminate",
                "left": {"task_ref": None, "version": None, "snapshot_created_at": None},
                "right": {"snapshot_created_at": "2026-10-10T00:00:00Z"},
                "changes": [], "ambiguities": [], "limitations": [],
                "network_scope": {"resource_metadata": False, "hls_and_head": False, "media_bodies": False}}
        original = deepcopy(data)
        result = envelope("tasks.check-updates", data)
        out, err = StringIO(), StringIO()
        render_result(result, json_output=False, stdout=out, stderr=err, output_dir=None)
        local, remote = out.getvalue().split("  远端观察", 1)
        self.assertIn("任务引用：未知", local)
        self.assertIn("版本：未知", local)
        self.assertIn("时间：未知", local)
        self.assertIn(data["task_id"], local)
        self.assertIn("时间：2026-10-10T00:00:00Z", remote)
        self.assertNotIn("任务引用：", remote)
        self.assertNotIn("版本：", remote)
        machine = StringIO()
        render_result(result, json_output=True, stdout=machine, stderr=err, output_dir=None)
        self.assertEqual(json.loads(machine.getvalue())["data"], original)
        self.assertEqual(data, original)

    def test_local_comparison_shows_each_full_reference_and_explicit_version(self):
        data = {"task_id": "a" * 64, "outcome": "no_observed_change",
                "left": {"task_ref": "a" * 64 + "@1", "version": 1, "snapshot_created_at": "first"},
                "right": {"task_ref": "a" * 64 + "@2", "version": 2, "snapshot_created_at": "second"}}
        out, err = StringIO(), StringIO()
        render_result(envelope("tasks.diff", data), json_output=False, stdout=out, stderr=err, output_dir=None)
        left, right = out.getvalue().split("  对照快照", 1)
        for section, side in ((left, data["left"]), (right, data["right"])):
            self.assertIn("任务引用：" + side["task_ref"], section)
            self.assertIn("版本：" + str(side["version"]), section)
            self.assertIn("时间：" + side["snapshot_created_at"], section)

    def test_human_fact_states_limitations_and_unknown_reason_remain_distinct(self):
        facts = [{"available": False, "present": False, "value": None},
                 {"available": True, "present": False, "value": None},
                 *[{"available": True, "present": True, "value": value} for value in (None, [], {}, "", False, 0)]]
        data = {"outcome": "indeterminate", "left": {"task_ref": "a" * 64 + "@1", "snapshot_created_at": None},
                "right": {"snapshot_created_at": None}, "changes": [
                    {"kind": "asset", "semantic_key": {"semantic_path": "artwork"}, "field": f"fact{i}",
                     "before": fact, "after": facts[-1]} for i, fact in enumerate(facts)],
                "ambiguities": [{"reason": "multiple_candidates", "semantic_key": {"semantic_path": "artwork"},
                                  "left_count": 2, "right_count": 3}],
                "limitations": [{"reason": "metadata_only"}, {"reason": "comparison_scope"},
                                {"reason": "declared_metadata_boundary"}, {"reason": "future_reason"}],
                "network_scope": {"resource_metadata": True, "hls_and_head": True, "media_bodies": False}}
        out, err = StringIO(), StringIO()
        render_result(envelope("tasks.check-updates", data), json_output=False, stdout=out, stderr=err, output_dir=None)
        for value in ("未能观察", "记录缺失", "null", "空数组", "空对象", "空字符串", "false", "0", "歧义",
                      "2", "3", "future_reason", "HLS", "媒体正文", "谎报", "--json"):
            self.assertIn(value, out.getvalue())
        self.assertNotIn("远端版本", out.getvalue())
        unknown, errors = StringIO(), StringIO()
        render_result(envelope("tasks.check-updates", {}), json_output=False,
                      stdout=unknown, stderr=errors, output_dir=None)
        self.assertIn("本次实际请求：未知", unknown.getvalue())

    def test_capabilities_and_help_use_actual_new_parsers(self):
        code, out, err = self.invoke(["capabilities", "--json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)["data"]
        for command in ("tasks diff", "tasks check-updates"):
            self.assertIn(command, data["commands"])
        self.assertEqual([item["name"] for item in data["arguments"]["tasks diff"][:2]], ["left_ref", "right_ref"])
        flags = [flag for item in data["arguments"]["tasks check-updates"] for flag in item["flags"]]
        self.assertIn("--token-env", flags)
        self.assertNotIn("--provider", flags)
        self.assertNotIn("--response", flags)
        self.assertEqual(data["protocol"]["comparison_outcomes"], ["observed_change", "no_observed_change", "indeterminate"])
        code, out, err = self.invoke(["tasks", "diff", "--help"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("LEFT", out)
        self.assertIn("RIGHT", out)


class CliUpdateMotionFailureTests(ComparisonCliSupport, TestCase):
    """Motion-extra CLI chain; base acceptance selects CliComparisonTests instead."""
    def test_hls_failure_retains_known_url_change_and_mapped_nonzero_exit(self):
        reference, _ = self.create(all_media=True)
        body = response()
        body["data"][0]["attributes"]["artwork"]["url"] = "https://a.mzstatic.com/changed/{w}x{h}.jpg"
        body["data"][0]["attributes"]["editorialVideo"] = {
            "motionDetailSquare": {"video": "https://mvod.itunes.apple.com/synthetic/master.m3u8"}}
        class FailedHls(MetadataTransport):
            def request(self, url, **options):
                if "mvod.itunes.apple.com" in url:
                    self.requests.append(("GET", url))
                    return HttpResponse(403, {"Content-Type": "application/vnd.apple.mpegurl"}, b"")
                return super().request(url, **options)
        for machine in (False, True):
            transport = FailedHls(body)
            code, out, err = self.invoke(["tasks", "check-updates", reference, "--library", str(self.library)] +
                                        (["--json"] if machine else []), transport=transport)
            self.assertEqual(code, 6, out + err)
            self.assertEqual(len(transport.requests), 2)
            if machine:
                result = json.loads(out)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["data"]["outcome"], "indeterminate")
                self.assertTrue(any(c["field"] == "url" and c["before"]["available"] and c["after"]["available"]
                                    for c in result["data"]["changes"]))
                self.assertTrue(result["data"]["network_scope"]["hls_and_head"])
            else:
                for text in ("无法判断", "地址变化", "请求失败", "HLS_HTTP_ERROR"):
                    self.assertIn(text, out + err)
