"""Frozen tasks, local reuse and crash recovery using synthetic media only."""

from argparse import Namespace
from copy import deepcopy
from pathlib import Path
from io import StringIO
import os
import signal
import tempfile
from unittest import TestCase
from unittest.mock import patch

from apple_music_artwork.client import HttpClient, HttpResponse
from apple_music_artwork.cli import main
from apple_music_artwork.download import OutputDirectory, validate_image
from apple_music_artwork.errors import ArtworkError
from apple_music_artwork.execution import execute
from apple_music_artwork.jsonio import dumps_json, loads_json
from apple_music_artwork.run_state import RunState, STATE_DIRECTORY
from test_download import image_bytes


class Crash(BaseException):
    pass


class Images:
    def __init__(self, *, bad=()):
        self.calls, self.bad = [], set(bad)

    def request(self, url, **kwargs):
        self.calls.append(url)
        body = b"invalid synthetic image" if any(value in url for value in self.bad) else image_bytes()
        return HttpResponse(200, {"Content-Type": "image/jpeg"}, body)


class NoNetwork:
    def request(self, *args, **kwargs):
        raise AssertionError("A completed task must not make any network request")


def response(count=1):
    def artwork(index):
        return {"url": f"https://is1-ssl.mzstatic.com/image/thumb/synthetic-{index}/{{w}}x{{h}}bb.jpg",
                "width": 4, "height": 4}
    attributes = {"artwork": artwork(0)}
    if count > 1:
        attributes["editorialArtwork"] = {"staticDetailSquare": artwork(1)}
    if count > 2:
        attributes["editorialArtwork"]["subscriptionHero"] = artwork(2)
    return {"data": [{"id": "1", "type": "albums", "href": "/v1/catalog/us/albums/1", "attributes": attributes}]}


class ExecutionTests(TestCase):
    @classmethod
    def setUpClass(cls):
        runtime = Path(tempfile.gettempdir()).resolve()
        assert runtime.resolve(strict=True) == runtime
        cls.scope = Path(tempfile.mkdtemp(prefix="v03-core-execution-", dir=runtime))

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="case-", dir=self.scope))
        self.response = self.folder / "response.json"
        self.response.write_text(dumps_json(response()), encoding="utf-8")
        self.output = self.folder / "output"
        self.args = Namespace(url="https://music.apple.com/us/album/synthetic/1?l=en-US", response=str(self.response),
                              output=str(self.output), list=False, semantic=None, asset_id=None, all_static=True,
                              all_media=False, all_motion=False, inspect_motion=False, compare_storefronts=None,
                              token_env="APPLE_MUSIC_DEVELOPER_TOKEN", provider="web", width=None, height=None,
                              format=None, motion_profile="best", jobs=2, progress="never", json=True)

    def run_task(self, transport=None, **kwargs):
        return execute(self.args, http=HttpClient(transport if transport is not None else Images()), environ={}, **kwargs)

    def test_complete_rerun_checks_hash_without_network_or_decoding(self):
        first, code = self.run_task()
        self.assertEqual(code, 0, first["errors"])
        original_validation = deepcopy(first["downloads"][0]["validation"])
        self.args.url = "https://music.apple.com/us/album/another-slug/1?l=en-US"
        self.args.jobs, self.args.progress, self.args.json = 4, "always", False
        with patch("apple_music_artwork.download.validate_image", side_effect=AssertionError("no decode")), \
             patch("apple_music_artwork.execution.validate_image", side_effect=AssertionError("no decode")):
            result, code = self.run_task(NoNetwork())
        self.assertEqual(code, 0, result["errors"])
        self.assertEqual(result["run"]["stats"]["requests"], 0)
        self.assertEqual(result["run"]["counts"]["reused"], 1)
        self.assertEqual(first["run"]["task_fingerprint"], result["run"]["task_fingerprint"])
        self.assertEqual(result["run"]["snapshot"]["remote_freshness"], "not_rechecked")
        self.assertEqual(result["downloads"][0]["validation"], original_validation)
        self.assertFalse(result["downloads"][0]["local_verification"]["decoded_this_run"])

    def test_only_missing_file_is_downloaded_and_changed_file_is_preserved(self):
        self.response.write_text(dumps_json(response(3)), encoding="utf-8")
        first, code = self.run_task()
        self.assertEqual(code, 0, first["errors"])
        (self.output / first["downloads"][0]["path"]).unlink()
        changed = self.output / first["downloads"][1]["path"]
        changed.write_bytes(b"user changed this file")
        transport = Images()
        result, code = self.run_task(transport)
        self.assertEqual(code, 6, result["errors"])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(changed.read_bytes(), b"user changed this file")
        self.assertEqual(result["run"]["counts"], {"total": 3, "new_downloaded": 1, "reused": 1,
                          "failed": 1, "cancelled": 0, "not_started": 0, "semantic_references": 3})

    def test_failed_validation_retries_only_on_next_invocation(self):
        self.response.write_text(dumps_json(response(2)), encoding="utf-8")
        transport = Images(bad={"synthetic-1"})
        first, code = self.run_task(transport)
        self.assertEqual(code, 7)
        self.assertEqual(len(transport.calls), 2)
        transport = Images()
        result, code = self.run_task(transport)
        self.assertEqual(code, 0, result["errors"])
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("synthetic-1", transport.calls[0])
        self.assertEqual(result["run"]["counts"]["reused"], 1)

    def test_parameters_and_offline_content_conflicts_stop_without_replacement(self):
        first, code = self.run_task()
        before = (self.output / "manifest.json").read_bytes()
        self.args.width = 2
        result, code = self.run_task(NoNetwork())
        self.assertEqual(code, 6)
        self.assertEqual(result["errors"][0]["code"], "TASK_CONFLICT")
        self.assertEqual((self.output / "manifest.json").read_bytes(), before)
        self.args.width = None
        payload = response()
        payload["data"][0]["attributes"]["name"] = "changed catalog snapshot"
        self.response.write_text(dumps_json(payload), encoding="utf-8")
        result, code = self.run_task(NoNetwork())
        self.assertEqual(code, 6)
        self.assertEqual(result["errors"][0]["code"], "TASK_CONFLICT")
        self.assertEqual((self.output / "manifest.json").read_bytes(), before)

    def test_corrupt_state_or_external_manifest_is_preserved(self):
        first, code = self.run_task()
        manifest = self.output / "manifest.json"
        manifest.write_bytes(b"external manifest")
        result, code = self.run_task(NoNetwork())
        self.assertEqual(result["errors"][0]["code"], "MANIFEST_CONFLICT")
        self.assertEqual(manifest.read_bytes(), b"external manifest")
        state = self.output / STATE_DIRECTORY / "state.json"
        state.write_bytes(b'{"duplicate":0,"duplicate":1}')
        result, code = self.run_task(NoNetwork())
        self.assertEqual(result["errors"][0]["code"], "RUN_STATE_INVALID")
        self.assertEqual(state.read_bytes(), b'{"duplicate":0,"duplicate":1}')

    def test_unknown_temporary_is_retained_and_reported(self):
        self.output.mkdir()
        unknown = self.output / (".apple-artwork-" + "a" * 32 + ".part")
        unknown.write_bytes(b"unregistered data")
        result, code = self.run_task()
        self.assertEqual(code, 0, result["errors"])
        self.assertIn(unknown.name, result["run"]["residue"])
        self.assertEqual(unknown.read_bytes(), b"unregistered data")

    def test_all_three_verified_file_crash_windows_recover_without_redownload(self):
        for phase in ("before_publish", "before_committed_checkpoint", "before_cleanup"):
            self.output = self.folder / phase
            self.args.output = str(self.output)
            original_publish, original_save, original_cleanup = OutputDirectory.publish_temp, RunState.save, OutputDirectory.cleanup_temp

            def publish(output, temporary, **kwargs):
                if temporary.final_name.endswith(".jpg") and phase == "before_publish":
                    raise Crash()
                return original_publish(output, temporary, **kwargs)

            def save(state, data=None):
                if phase == "before_committed_checkpoint" and any(item.get("status") == "committed" for item in (state.data or {}).get("files", {}).values()):
                    raise Crash()
                return original_save(state, data)

            def cleanup(output, temporary):
                if temporary.final_name.endswith(".jpg") and phase == "before_cleanup":
                    raise Crash()
                return original_cleanup(output, temporary)

            with self.subTest(phase=phase), patch.object(OutputDirectory, "publish_temp", publish), \
                 patch.object(RunState, "save", save), patch.object(OutputDirectory, "cleanup_temp", cleanup):
                with self.assertRaises(Crash):
                    self.run_task()
            with patch("apple_music_artwork.download.validate_image", side_effect=AssertionError("no repeated decode")):
                result, code = self.run_task(NoNetwork())
            self.assertEqual(code, 0, result["errors"])
            self.assertEqual(result["run"]["stats"]["requests"], 0)
            self.assertEqual(result["run"]["counts"]["new_downloaded"] + result["run"]["counts"]["reused"], 1)
            self.assertEqual(list(self.output.glob(".apple-artwork-*.part")), [])

    def test_interrupted_unvalidated_file_is_removed_then_downloaded_whole(self):
        def partial(asset, request, temporary, http, **kwargs):
            temporary.write_stream(lambda stream: stream.write(b"partial"))
            raise Crash()
        with patch("apple_music_artwork.execution.prepare_image", partial):
            with self.assertRaises(Crash):
                self.run_task()
        self.assertEqual(len(list(self.output.glob(".apple-artwork-*.part"))), 1)
        transport = Images()
        result, code = self.run_task(transport)
        self.assertEqual(code, 0, result["errors"])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(list(self.output.glob(".apple-artwork-*.part")), [])
        self.assertEqual((self.output / result["downloads"][0]["path"]).read_bytes(), image_bytes())

    def test_online_legacy_import_decodes_once_and_records_unknown_snapshot_time(self):
        self.args.response = None
        with patch("apple_music_artwork.execution.AppleMusicClient.fetch", return_value=response()):
            first, code = self.run_task()
        legacy_output = self.folder / "legacy"
        legacy_output.mkdir()
        old = loads_json((self.output / "manifest.json").read_bytes())
        old.pop("run")
        for entry in old["downloads"]:
            entry.pop("action")
            (legacy_output / entry["path"]).write_bytes((self.output / entry["path"]).read_bytes())
        (legacy_output / "manifest.json").write_text(dumps_json(old), encoding="utf-8")
        self.output, self.args.output = legacy_output, str(legacy_output)
        with patch("apple_music_artwork.execution.AppleMusicClient.fetch", side_effect=AssertionError("no catalog refresh")), \
             patch("apple_music_artwork.execution.validate_image", wraps=validate_image) as decoder:
            result, code = self.run_task(NoNetwork())
        self.assertEqual(code, 0, result["errors"])
        self.assertEqual(decoder.call_count, 1)
        self.assertIsNone(result["run"]["snapshot"]["created_at"])
        self.assertTrue(result["downloads"][0]["local_verification"]["decoded_this_run"])
        self.assertEqual(result["downloads"][0]["validation"], old["downloads"][0]["validation"])
        with patch("apple_music_artwork.execution.validate_image", side_effect=AssertionError("no repeated local decode")):
            result, code = self.run_task(NoNetwork())
        self.assertEqual(code, 0, result["errors"])
        self.assertFalse(result["downloads"][0]["local_verification"]["decoded_this_run"])

    def test_offline_legacy_manifest_is_not_guessed_from_current_response(self):
        first, code = self.run_task()
        legacy = loads_json((self.output / "manifest.json").read_bytes())
        legacy.pop("run")
        other = self.folder / "legacy-offline"
        other.mkdir()
        original = dumps_json(legacy).encode()
        (other / "manifest.json").write_bytes(original)
        self.args.output = str(other)
        result, code = self.run_task(NoNetwork())
        self.assertEqual(code, 6)
        self.assertEqual(result["errors"][0]["code"], "LEGACY_MANIFEST_UNRECOGNIZED")
        self.assertEqual((other / "manifest.json").read_bytes(), original)

    def cli(self, transport=None):
        stdout, stderr = StringIO(), StringIO()
        code = main([self.args.url, "--response", str(self.response), "--all-static", "--jobs", "1",
                     "--progress", "never", "--json", "-o", str(self.output)],
                    transport=transport if transport is not None else Images(), environ={}, stdout=stdout, stderr=stderr)
        return code, loads_json(stdout.getvalue()), stderr.getvalue()

    def test_cli_cancellation_separates_active_and_unstarted_files_from_failures(self):
        self.response.write_text(dumps_json(response(3)), encoding="utf-8")

        class CancelFirst(Images):
            def request(self, url, **kwargs):
                os.kill(os.getpid(), signal.SIGINT)
                return super().request(url, **kwargs)

        transport = CancelFirst()
        code, result, stderr = self.cli(transport)
        self.assertEqual(code, 130)
        self.assertEqual(stderr, "")
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(result["run"]["cancelled"])
        self.assertEqual(result["downloads"], [])
        self.assertEqual([error["code"] for error in result["errors"]], ["CANCELLED"])
        self.assertEqual(result["run"]["counts"]["cancelled"], 1)
        self.assertEqual(result["run"]["counts"]["not_started"], 2)
        self.assertEqual(len(result["run"]["interrupted"]), 3)
        self.assertEqual(list(self.output.glob(".apple-artwork-*.part")), [])

    def test_cli_internal_checkpoint_error_is_not_reclassified_as_user_cancellation(self):
        self.response.write_text(dumps_json(response(3)), encoding="utf-8")
        original = RunState.save

        def failed(state, data=None):
            if any(record.get("status") == "running" for record in (state.data or {}).get("files", {}).values()):
                raise RuntimeError("synthetic internal checkpoint failure")
            return original(state, data)

        transport = Images()
        with patch.object(RunState, "save", failed):
            code, result, stderr = self.cli(transport)
        self.assertEqual(code, 1)
        self.assertFalse(result["run"]["cancelled"])
        self.assertEqual(transport.calls, [])
        self.assertEqual(result["downloads"], [])
        self.assertEqual([error["code"] for error in result["errors"]], ["INTERNAL_ERROR"])
        self.assertEqual(result["run"]["counts"]["not_started"], 3)
        self.assertEqual(len(result["run"]["residue"]), 1)

    def test_repeated_sigint_during_commit_finishes_already_validated_file(self):
        self.response.write_text(dumps_json(response(3)), encoding="utf-8")
        original = OutputDirectory.publish_temp

        def interrupted(output, temporary, **kwargs):
            if not temporary.final_name.endswith(".jpg"):
                return original(output, temporary, **kwargs)
            os.kill(os.getpid(), signal.SIGINT)
            result = original(output, temporary, **kwargs)
            os.kill(os.getpid(), signal.SIGINT)
            return result

        with patch.object(OutputDirectory, "publish_temp", interrupted):
            code, result, stderr = self.cli()
        self.assertEqual(code, 130)
        self.assertEqual(result["run"]["counts"]["new_downloaded"], 1)
        self.assertEqual(result["run"]["counts"]["not_started"], 2)
        self.assertEqual(len(result["downloads"]), 1)
        self.assertEqual(result["downloads"][0]["status"], "downloaded")
        self.assertEqual(result["manifest_report"]["status"], "saved")
        self.assertEqual(list(self.output.glob(".apple-artwork-*.part")), [])

    def test_prepared_recovery_with_old_policy_requires_current_local_validation(self):
        original = OutputDirectory.publish_temp

        def crash(output, temporary, **kwargs):
            if temporary.final_name.endswith(".jpg"):
                raise Crash()
            return original(output, temporary, **kwargs)

        with patch.object(OutputDirectory, "publish_temp", crash):
            with self.assertRaises(Crash):
                self.run_task()
        with OutputDirectory(self.output) as output, RunState(output) as state:
            record = next(iter(state.data["files"].values()))
            record["validation_policy"] = "older-image-decode-v0"
            state.save()
        with patch("apple_music_artwork.execution.validate_image", wraps=validate_image) as decoder:
            result, code = self.run_task(NoNetwork())
        self.assertEqual(code, 0, result["errors"])
        self.assertEqual(decoder.call_count, 1)
        self.assertEqual(result["run"]["stats"]["requests"], 0)
        with OutputDirectory(self.output) as output, RunState(output) as state:
            record = next(iter(state.data["files"].values()))
            self.assertEqual(record["validation_policy"], "image-decode-v1")
            self.assertTrue(record["local_verification"]["decoded_this_run"])

