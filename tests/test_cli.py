"""End-to-end synthetic catalog -> policy -> decoded files -> manifest tests."""

from http.client import HTTPResponse
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import tempfile
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch

from PIL import Image

from apple_music_artwork.cli import main
from apple_music_artwork.client import HttpClient, HttpResponse, UrllibTransport
from apple_music_artwork.download import OutputDirectory


URL = "https://music.apple.com/us/album/example/1"
ART_URL = "https://is1-ssl.mzstatic.com/image/thumb/source/{w}x{h}bb.jpg"


def payload(second=None):
    attributes = {"name": "Synthetic album", "artwork": {"url": ART_URL, "width": 4, "height": 4}}
    if second:
        attributes["editorialArtwork"] = {"staticDetailSquare": {"url": second, "width": 4, "height": 4}}
    return {"data": [{"id": "1", "type": "albums", "attributes": attributes}]}


def image_reply():
    stream = BytesIO()
    with Image.new("RGB", (4, 4), (34, 68, 102)) as image:
        image.save(stream, format="JPEG")
    return HttpResponse(200, {"Content-Type": "image/jpeg"}, stream.getvalue())


class QueueTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def request(self, url, **kwargs):
        self.urls.append(url)
        if not self.responses:
            raise AssertionError("Unexpected network request")
        return self.responses.pop(0)


class CliInputTests(TestCase):
    def run_cli(self, args, **kwargs):
        stdout, stderr = StringIO(), StringIO()
        temporary = tempfile.TemporaryDirectory(prefix="artwork-cli-input-", dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "output"
        code = main([*args, "-o", str(output)], stdout=stdout, stderr=stderr, environ={}, **kwargs)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_help_and_version_use_requested_stream(self):
        for args, expected in [(["--help"], "实验性公开 WebPlay"), (["--version"], "0.5.0")]:
            code, stdout, stderr = self.run_cli(args)
            self.assertEqual(code, 0)
            self.assertIn(expected, stdout)
            self.assertEqual(stderr, "")

    def test_argument_and_url_errors_are_single_json_without_echo(self):
        for args in (["--json", URL, "--width", "private-secret"], ["--json", "https://user:private-secret@music.apple.com/us/album/example/1"]):
            code, stdout, stderr = self.run_cli(args)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(stdout)["status"], "failed")
            self.assertNotIn("private-secret", stdout)
            self.assertEqual(stderr, "")

    def test_catalog_missing_token_is_auth_exit_without_http(self):
        transport = QueueTransport()
        code, stdout, _ = self.run_cli([URL, "--provider", "catalog", "--json"], transport=transport)
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(stdout)["errors"][0]["code"], "TOKEN_REQUIRED")
        self.assertFalse(transport.urls)

    def test_unexpected_exceptions_are_redacted(self):
        with patch("apple_music_artwork.cli._execute", side_effect=RuntimeError("private-secret")):
            code, stdout, _ = self.run_cli([URL, "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout)["errors"][0]["code"], "INTERNAL_ERROR")
        self.assertNotIn("private-secret", stdout)


@skipUnless(os.name == "posix", "Safe file publication currently requires POSIX")
class CliFlowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="artwork-cli-flow-", dir=Path(tempfile.gettempdir()).resolve())
        cls.addClassCleanup(temporary.cleanup)
        cls.scope = Path(temporary.name)

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="case-", dir=self.scope))
        self.response = self.folder / "response.json"
        self.output = self.folder / "output"

    def write_response(self, value=None):
        with self.response.open("x", encoding="utf-8") as stream:
            json.dump(payload() if value is None else value, stream)

    def invoke(self, *args, transport=None, environ=None, http=None):
        stdout, stderr = StringIO(), StringIO()
        jobs = [] if "--list" in args else ["--jobs", "1"]
        code = main([URL, "--json", "--output", str(self.output), *jobs, *args],
                    transport=transport or QueueTransport(), http=http, environ=environ or {}, stdout=stdout, stderr=stderr)
        self.assertEqual(stderr.getvalue(), "")
        return code, json.loads(stdout.getvalue())

    def offline(self, *args, transport=None, http=None):
        return self.invoke("--response", str(self.response), *args, transport=transport, http=http)

    def test_list_is_offline_and_creates_no_output(self):
        self.write_response()
        transport = QueueTransport()
        code, result = self.offline("--list", transport=transport)
        self.assertEqual(code, 0)
        self.assertEqual((result["schema_version"], result["mode"], result["provider"]), (2, "list", "offline"))
        self.assertEqual(result["assets"][0]["observations"][0]["variant"], "import")
        self.assertIsNone(result["selection"])
        self.assertFalse(self.output.exists())
        self.assertFalse(transport.urls)

    def test_online_catalog_to_decoded_image_and_persisted_manifest(self):
        transport = QueueTransport(HttpResponse(200, {}, json.dumps(payload()).encode()), image_reply())
        code, result = self.invoke("--provider", "catalog", "--token-env", "SYNTHETIC_TOKEN",
                                   transport=transport, environ={"SYNTHETIC_TOKEN": "synthetic-token"})
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["provider"], "catalog")
        self.assertEqual(result["selection"]["policy"], "album-primary-v1")
        self.assertEqual(len(transport.urls), 2)
        item = result["downloads"][0]
        self.assertTrue(item["validation"]["decoded"])
        self.assertTrue((self.output / item["path"]).is_file())
        persisted = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual({key: value for key, value in result.items() if key != "manifest_report"}, persisted)
        serialized = json.dumps(result)
        self.assertNotIn("synthetic-token", serialized)
        self.assertNotIn(str(self.folder), serialized)

    def test_manifest_conflict_prevents_any_image_request(self):
        self.write_response()
        self.output.mkdir()
        with (self.output / "manifest.json").open("x") as stream:
            stream.write("existing manifest")
        transport = QueueTransport()
        code, result = self.offline(transport=transport)
        self.assertEqual(code, 6)
        self.assertEqual(result["errors"][0]["code"], "LEGACY_MANIFEST_UNRECOGNIZED")
        self.assertEqual((self.output / "manifest.json").read_text(), "existing manifest")
        self.assertFalse(transport.urls)

    def test_all_static_partial_failure_saves_successful_file_and_manifest(self):
        self.write_response(payload(ART_URL.replace("source", "second-source")))
        transport = QueueTransport(image_reply(), HttpResponse(200, {"Content-Type": "text/html"}, b"<html/>"))
        code, result = self.offline("--all-static", transport=transport)
        self.assertEqual(code, 8)
        self.assertEqual(result["control"]["lifecycle"], "needs_action")
        self.assertTrue(result["control"]["decision_required"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual([item["status"] for item in result["downloads"]], ["downloaded", "failed"])
        self.assertTrue((self.output / result["downloads"][0]["path"]).is_file())
        self.assertEqual(json.loads((self.output / "manifest.json").read_text())["status"], "partial")

    def test_truncated_chunked_second_image_preserves_first_and_partial_manifest(self):
        self.write_response(payload(ART_URL.replace("source", "second-source")))
        class GoodResponse(BytesIO):
            status = 200
            headers = {"Content-Type": "image/jpeg"}
        class SocketBytes:
            def makefile(self, mode):
                return BytesIO(b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nTransfer-Encoding: chunked\r\n\r\n10\r\nabc")
        transport = UrllibTransport()
        transport._opener = Mock()
        broken_responses = [HTTPResponse(SocketBytes()) for _ in range(3)]
        for response in broken_responses:
            response.begin()
        transport._opener.open.side_effect = [GoodResponse(image_reply().body), *broken_responses]
        clone = UrllibTransport()
        clone._opener = transport._opener
        with patch.object(transport, "fork", return_value=clone):
            code, result = self.offline("--all-static", http=HttpClient(transport, sleep=lambda seconds: None))
        self.assertEqual(code, 8)
        self.assertEqual(result["control"]["lifecycle"], "needs_action")
        self.assertEqual(result["status"], "partial")
        self.assertEqual([item["status"] for item in result["downloads"]], ["downloaded", "failed"])
        self.assertEqual(result["downloads"][1]["error"]["code"], "NETWORK_ERROR")
        self.assertTrue((self.output / result["downloads"][0]["path"]).is_file())
        self.assertEqual(json.loads((self.output / "manifest.json").read_text())["status"], "partial")
        self.assertEqual(transport._opener.open.call_count, 2)

    def test_only_exact_resolved_renditions_deduplicate_and_keep_references(self):
        self.write_response(payload(ART_URL))
        transport = QueueTransport(image_reply())
        code, result = self.offline("--all-static", transport=transport)
        self.assertEqual(code, 0)
        self.assertEqual(len(result["selection"]["selected"]), 2)
        self.assertEqual(len(result["downloads"]), 1)
        self.assertEqual(len(result["downloads"][0]["references"]), 2)
        self.assertEqual(len(transport.urls), 1)

    def test_same_source_distinct_renditions_both_download(self):
        self.write_response(payload(ART_URL.replace("bb.jpg", "SC.DN01.jpg")))
        transport = QueueTransport(image_reply(), image_reply())
        code, result = self.offline("--all-static", transport=transport)
        self.assertEqual(code, 0)
        self.assertEqual(len(result["downloads"]), 2)
        self.assertEqual(len({item["source_identity"] for item in result["downloads"]}), 1)
        self.assertEqual(len({item["path"] for item in result["downloads"]}), 2)

    def test_fixed_format_is_preserved_and_preference_is_reported(self):
        self.write_response()
        code, result = self.offline("--format", "png", transport=QueueTransport(image_reply()))
        self.assertEqual(code, 0)
        self.assertEqual(result["options"]["format"], "png")
        self.assertEqual(result["downloads"][0]["requested_format"], "jpg")
        self.assertEqual(result["downloads"][0]["validation"]["actual_format"], "JPEG")

    def test_image_cleanup_failure_reports_committed_image_and_partial(self):
        self.write_response()
        original = OutputDirectory.cleanup_temp
        def fail_image_cleanup(output, temporary):
            if temporary.final_name.endswith(".jpg"):
                with patch("apple_music_artwork.download.os.unlink", side_effect=PermissionError("test")):
                    return original(output, temporary)
            return original(output, temporary)
        with patch.object(OutputDirectory, "cleanup_temp", fail_image_cleanup):
            code, result = self.offline(transport=QueueTransport(image_reply()))
        self.assertEqual(code, 6)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["downloads"][0]["status"], "downloaded")
        self.assertTrue(result["downloads"][0]["validation"]["decoded"])
        self.assertTrue(result["downloads"][0]["output"]["committed"])
        self.assertTrue(result["downloads"][0]["output"]["residue"])
        self.assertEqual(json.loads((self.output / "manifest.json").read_text())["status"], "partial")

    def test_manifest_postcommit_failure_is_separate_from_download_snapshot(self):
        self.write_response()
        original = OutputDirectory.cleanup_temp
        def fail_manifest_cleanup(output, temporary):
            if temporary.final_name == "manifest.json":
                with patch("apple_music_artwork.download.os.unlink", side_effect=PermissionError("test")):
                    return original(output, temporary)
            return original(output, temporary)
        with patch.object(OutputDirectory, "cleanup_temp", fail_manifest_cleanup):
            code, result = self.offline(transport=QueueTransport(image_reply()))
        self.assertEqual(code, 6)
        self.assertEqual((result["status"], result["status_scope"], result["download_status"]), ("partial", "runtime", "success"))
        self.assertTrue(result["manifest_report"]["committed"])
        self.assertTrue(result["manifest_report"]["residue"])
        persisted = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual((persisted["status"], persisted["status_scope"]), ("success", "download_results"))
        self.assertNotIn("manifest_report", persisted)

    def test_manifest_precommit_failure_keeps_completed_image(self):
        self.write_response()
        original = OutputDirectory.publish_temp
        def fail_manifest_commit(output, temporary, **kwargs):
            if temporary.final_name == "manifest.json":
                with patch("apple_music_artwork.download.os.link", side_effect=PermissionError("test")):
                    return original(output, temporary, **kwargs)
            return original(output, temporary, **kwargs)
        with patch.object(OutputDirectory, "publish_temp", fail_manifest_commit):
            code, result = self.offline(transport=QueueTransport(image_reply()))
        self.assertEqual(code, 6)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["manifest_report"]["committed"])
        self.assertFalse((self.output / "manifest.json").exists())
        self.assertTrue((self.output / result["downloads"][0]["path"]).is_file())

    def test_invalid_rendition_saves_failed_attempt_without_image_fetch(self):
        self.write_response()
        transport = QueueTransport()
        code, result = self.offline("--width", "100", transport=transport)
        self.assertEqual(code, 8)
        self.assertEqual(result["control"]["lifecycle"], "needs_action")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errors"][0]["code"], "DIMENSIONS_EXCEED_DECLARED")
        self.assertTrue((self.output / "manifest.json").is_file())
        self.assertFalse(transport.urls)
