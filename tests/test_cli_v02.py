"""Version-two result contracts with injected metadata, media and output boundaries."""

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from PIL import Image

from apple_music_artwork.cli import main
from apple_music_artwork.execution import _comparison_signature
from apple_music_artwork.client import HttpResponse
from apple_music_artwork.errors import ArtworkError
from apple_music_artwork.download import prepare_image
from apple_music_artwork.hls import MotionInspection, MotionLayout, MotionVariant


URL = "https://music.apple.com/us/album/example/1"
IMAGE_URL = "https://is1-ssl.mzstatic.com/image/thumb/synthetic/{w}x{h}bb.{f}"
MOTION_URL = "https://mvod.itunes.apple.com/itunes-assets/synthetic/master.m3u8"


def payload(*, storefront="us", artwork=True, motion=False):
    attributes = {"editorialArtwork": {}, "editorialVideo": {}}
    if artwork:
        attributes["artwork"] = {"url": IMAGE_URL, "width": 4, "height": 4}
    if motion:
        attributes["editorialVideo"] = {"motionDetailSquare": {"video": MOTION_URL}}
    return {"data": [{"id": "1", "type": "albums", "href": f"/v1/catalog/{storefront}/albums/1",
                      "attributes": attributes}]}


def image_bytes(format):
    stream = BytesIO()
    with Image.new("RGBA" if format == "PNG" else "RGB", (4, 4), (31, 62, 93)) as image:
        image.save(stream, format=format)
    return stream.getvalue()


class ImageTransport:
    def __init__(self):
        self.requests = []

    def request(self, url, **kwargs):
        self.requests.append(url)
        format = "PNG" if url.endswith(".png") else "JPEG"
        return HttpResponse(200, {"Content-Type": "image/png" if format == "PNG" else "image/jpeg"}, image_bytes(format))


class ResultContractTests(TestCase):
    def run_cli(self, *args, value=None, fetch=None, transport=None, media_tools=None, environ=None, url=URL):
        stdout, stderr = StringIO(), StringIO()
        metadata = patch("apple_music_artwork.execution.AppleMusicClient.fetch", side_effect=fetch) if fetch else patch(
            "apple_music_artwork.execution.AppleMusicClient.fetch", return_value=payload() if value is None else value)
        temporary = tempfile.TemporaryDirectory(prefix="artwork-cli-contract-", dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temporary.cleanup)
        self.last_output = Path(temporary.name) / "output"
        jobs = [] if "--list" in args else ["--jobs", "1"]
        with metadata:
            code = main([url, "--json", "-o", str(self.last_output), *jobs, *args], stdout=stdout, stderr=stderr,
                        transport=transport, media_tools=media_tools, environ={} if environ is None else environ)
        self.assertEqual(stderr.getvalue(), "")
        return code, json.loads(stdout.getvalue())

    def assert_no_business_output(self):
        names = {path.name for path in self.last_output.iterdir()} if self.last_output.exists() else set()
        self.assertLessEqual(names, {".apple-artwork-state"})

    def test_selection_failure_preserves_discovered_assets_and_coverage(self):
        value = payload(artwork=False)
        value["data"][0]["attributes"]["editorialArtwork"]["staticDetailSquare"] = {
            "url": IMAGE_URL, "width": 4, "height": 4,
        }
        code, result = self.run_cli(value=value)
        self.assertEqual(code, 5)
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["errors"][0]["code"], "NO_PRIMARY_STATIC_ASSET")
        self.assertEqual(len(result["assets"]), 1)
        self.assertEqual(result["target"]["key"]["storefront"], "us")
        self.assertEqual(result["coverage"]["fields"]["artwork"]["state"], "missing")
        self.assertEqual(result["manifest_report"]["status"], "not_saved")
        self.assert_no_business_output()

    def test_conflicting_storefront_preserves_observed_owner_and_prevents_output(self):
        code, result = self.run_cli(value=payload(storefront="us"), url=URL.replace("/us/", "/cn/"))
        self.assertEqual(code, 5)
        self.assertEqual(result["errors"][0]["code"], "RESOURCE_STOREFRONT_CONFLICT")
        self.assertEqual(result["assets"][0]["owner"]["storefront"], "us")
        self.assertEqual(result["source_evidence"]["status"], "conflict")
        self.assert_no_business_output()

    def test_field_presence_and_execution_status_remain_separate(self):
        for state, value in (("missing", object()), ("null", None), ("empty", {})):
            with self.subTest(state=state):
                data = payload()
                if state == "missing":
                    del data["data"][0]["attributes"]["editorialVideo"]
                else:
                    data["data"][0]["attributes"]["editorialVideo"] = value
                code, result = self.run_cli("--list", value=data)
                self.assertEqual(code, 0)
                self.assertEqual(result["status"], "success")
                self.assertEqual(result["coverage"]["fields"]["editorialVideo"]["state"], state)
                self.assertEqual(result["coverage"]["completeness"]["status"], "incomplete")

    def test_compare_requires_explicit_locale_and_never_reuses_an_offline_response(self):
        for args in (("--list", "--compare-storefronts", "cn"),
                     ("--list", "--response", "unused.json", "--compare-storefronts", "cn"),
                     ("--compare-storefronts", "cn")):
            fetch = Mock(side_effect=AssertionError("No request should occur"))
            code, result = self.run_cli(*args, fetch=fetch)
            self.assertEqual(code, 2)
            self.assertEqual(result["status"], "failed")
            fetch.assert_not_called()

    def test_compare_keeps_locale_owners_and_reports_declaration_differences(self):
        targets = []

        def fetch(target):
            targets.append(target)
            value = payload(storefront=target.key.storefront)
            value["data"][0]["attributes"]["artwork"]["hasP3"] = target.key.storefront == "cn"
            return value

        code, result = self.run_cli("--list", "--compare-storefronts", "cn", "us", fetch=fetch,
                                    url=URL + "?l=zh-Hans-CN")
        self.assertEqual(code, 0)
        self.assertEqual([target.key.storefront for target in targets], ["us", "cn"])
        self.assertEqual({target.locale for target in targets}, {"zh-Hans-CN"})
        comparison = result["comparison"]
        self.assertEqual([item["assets"][0]["owner"]["storefront"] for item in comparison["results"]], ["us", "cn"])
        self.assertEqual(len(comparison["differences"][0]["only_in_input"]), 1)
        self.assertEqual(len(comparison["differences"][0]["only_in_compared"]), 1)

    def test_comparison_signature_ignores_observations_only(self):
        asset = {"semantic_path": "artwork", "kind": "image", "role": "primary_static", "url": IMAGE_URL,
                 "width": 4, "height": 4, "traits": [], "declarations": [{"name": "hasP3", "value": True,
                                                                           "observations": [{"variant": "one"}]}]}
        other = deepcopy(asset)
        other["declarations"][0]["observations"] = [{"variant": "two", "json_pointer": "/another"}]
        self.assertEqual(_comparison_signature(asset), _comparison_signature(other))
        other["declarations"][0]["value"] = False
        self.assertNotEqual(_comparison_signature(asset), _comparison_signature(other))

    def test_compare_failure_keeps_successful_storefront_result(self):
        def fetch(target):
            if target.key.storefront == "cn":
                raise ArtworkError("RESOURCE_NOT_FOUND", "Synthetic unavailable storefront.", "api")
            return payload(storefront=target.key.storefront)

        code, result = self.run_cli("--list", "--compare-storefronts", "cn", fetch=fetch, url=URL + "?l=en-US")
        self.assertEqual(code, 4)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["assets"]), 1)
        self.assertEqual(result["comparison"]["results"][0]["status"], "success")
        self.assertEqual(result["comparison"]["differences"][0]["status"], "unavailable")

    def test_new_modes_remain_mutually_exclusive_and_list_only_options_are_checked(self):
        for args in (("--all-media", "--all-static"), ("--all-motion", "--semantic", "artwork"),
                     ("--list", "--all-media"), ("--list", "--all-motion"), ("--inspect-motion",)):
            with self.subTest(args=args):
                code, result = self.run_cli(*args)
                self.assertEqual(code, 2)
                self.assertEqual(result["errors"][0]["code"], "INVALID_ARGUMENTS")

    def test_all_media_png_default_and_old_jpeg_default_use_real_decoded_bytes(self):
        for args, expected in ((("--all-media",), "PNG"), ((), "JPEG"), (("--all-static",), "JPEG"),
                               (("--all-media", "--format", "jpg"), "JPEG")):
            with self.subTest(args=args):
                code, result = self.run_cli(*args, transport=ImageTransport())
            self.assertEqual(code, 0)
            self.assertEqual(result["downloads"][0]["validation"]["actual_format"], expected)
            self.assertEqual(result["coverage"]["completeness"]["status"], "complete")

    def test_aliases_pass_all_color_declarations_to_image_validation(self):
        value = payload()
        attributes = value["data"][0]["attributes"]
        attributes["artwork"]["hasP3"] = False
        attributes["editorialArtwork"]["staticDetailSquare"] = {**attributes["artwork"], "hasP3": True}
        calls = []

        def download(*args, declared_p3, **kwargs):
            calls.append(declared_p3)
            return prepare_image(*args, declared_p3=declared_p3, **kwargs)

        with patch("apple_music_artwork.execution.prepare_image", side_effect=download):
            code, result = self.run_cli("--all-media", value=value, transport=ImageTransport())
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0]), {False, True})
        self.assertEqual(len(result["downloads"][0]["references"]), 2)


def motion_inspection(asset, http=None, profile="best", mp4_url="https://mvod.itunes.apple.com/synthetic/one.mp4"):
    dimension = 1080 if profile == "compatible" else 2160
    codec = "avc1.640028" if profile == "compatible" else "hvc1.2.4.L120.B0"
    variant = MotionVariant(MOTION_URL, dimension, dimension, codec, 30.0, 9000, 10000)
    layout = MotionLayout(mp4_url, mp4_url, MOTION_URL, 100, 2.0, 1, ((0, 4), (4, 96)))
    return MotionInspection(asset.asset_id, asset.url, profile, "supported", (variant,), variant, layout,
                            resolved_master_url=asset.url)


def motion_artifact(asset, inspection, temporary, http, tools, **kwargs):
    # This is only a coordinator contract fixture; real-tool acceptance uses
    # owned MP4 bytes in the separate explicit integration script.
    body = b"synthetic-motion-contract".ljust(100, b".")
    validation = {"decoded": True, "full_decode": True, "actual_format": "MP4",
                  "actual_dimensions": {"width": inspection.selected.width, "height": inspection.selected.height},
                  "actual_codec": inspection.selected.codec,
                  "bytes": len(body), "sha256": sha256(body).hexdigest()}
    temporary.write_stream(lambda stream: stream.write(body), lambda path, count: validation)
    return {"asset_id": asset.asset_id, "path": temporary.final_name, "status": "downloaded",
            "url": inspection.layout.mp4_url, "profile": inspection.profile,
            "layout": inspection.layout.to_dict(), "selected_variant": inspection.selected.to_dict(),
            "validation": validation}


class MotionCliContractTests(TestCase):
    run_cli = ResultContractTests.run_cli
    assert_no_business_output = ResultContractTests.assert_no_business_output

    def setUp(self):
        self.tools = SimpleNamespace(to_dict=lambda: {"ffmpeg": {"version": "synthetic"}, "ffprobe": {"version": "synthetic"}})

    def test_static_mode_does_not_require_parser_or_media_tools(self):
        with patch("apple_music_artwork.hls.require_parser") as parser, \
                patch("apple_music_artwork.motion.MediaTools.discover") as tools:
            code, result = self.run_cli(transport=ImageTransport())
        self.assertEqual(code, 0)
        self.assertEqual(result["downloads"][0]["validation"]["actual_format"], "JPEG")
        parser.assert_not_called()
        tools.assert_not_called()

    def test_missing_motion_dependency_preflight_prevents_business_output(self):
        for dependency in ("parser", "tools"):
            failure = ArtworkError("MOTION_DEPENDENCY_MISSING" if dependency == "parser" else "MEDIA_TOOL_MISSING",
                                   "Synthetic missing dependency.", "download")
            transport = ImageTransport()
            with self.subTest(dependency=dependency), \
                    patch("apple_music_artwork.hls.require_parser", side_effect=failure if dependency == "parser" else None), \
                    patch("apple_music_artwork.motion.MediaTools.discover", side_effect=failure if dependency == "tools" else None), \
                    patch("apple_music_artwork.hls.inspect_motion") as inspect:
                code, result = self.run_cli("--all-media", value=payload(motion=True), transport=transport)
            self.assertEqual(code, 6)
            self.assertEqual(result["errors"][0]["code"], failure.code)
            self.assertEqual(len(result["assets"]), 2)
            self.assertEqual(len(result["selection"]["selected"]), 2)
            self.assertEqual(result["downloads"], [])
            self.assertFalse(transport.requests)
            self.assert_no_business_output()
            inspect.assert_not_called()

    def test_list_inspection_uses_no_media_tools_or_output(self):
        with patch("apple_music_artwork.hls.inspect_motion", side_effect=motion_inspection) as inspect, \
                patch("apple_music_artwork.motion.MediaTools.discover") as tools:
            code, result = self.run_cli("--list", "--inspect-motion", value=payload(motion=True))
        self.assertEqual(code, 0)
        self.assertEqual(inspect.call_count, 1)
        self.assertFalse(result["motion_inspections"][0]["inspection"]["media_body_checked"])
        self.assertEqual(result["downloads"], [])
        self.assertEqual(result["coverage"]["completeness"]["status"], "incomplete")
        self.assert_no_business_output()
        tools.assert_not_called()

    def test_motion_aliases_deduplicate_and_static_options_do_not_resize_video(self):
        value = payload(motion=True)
        value["data"][0]["attributes"]["editorialVideo"]["motionSquareVideo1x1"] = {"video": MOTION_URL}
        with patch("apple_music_artwork.hls.require_parser"), \
                patch("apple_music_artwork.hls.inspect_motion", side_effect=motion_inspection) as inspect, \
                patch("apple_music_artwork.motion.prepare_motion", side_effect=motion_artifact) as download:
            code, result = self.run_cli("--all-motion", "--motion-profile", "compatible", "--width", "1", "--height", "1",
                                        "--format", "avif", value=value, media_tools=self.tools)
        self.assertEqual(code, 0)
        self.assertEqual(inspect.call_count, 1)
        self.assertEqual(inspect.call_args.kwargs["profile"], "compatible")
        self.assertEqual(download.call_count, 1)
        entry = result["downloads"][0]
        self.assertEqual(len(entry["references"]), 2)
        self.assertEqual(entry["validation"]["actual_dimensions"], {"width": 1080, "height": 1080})
        self.assertEqual(result["coverage"]["completeness"]["status"], "incomplete")

    def test_distinct_motion_sources_with_same_shape_are_both_downloaded(self):
        value = payload(motion=True)
        other_url = "https://mvod.itunes.apple.com/itunes-assets/synthetic/another.m3u8"
        value["data"][0]["attributes"]["editorialVideo"]["motionSquareVideo1x1"] = {"video": other_url}

        def inspect(asset, http, profile):
            mp4_url = "https://mvod.itunes.apple.com/synthetic/one.mp4" if asset.url == MOTION_URL else "https://mvod.itunes.apple.com/synthetic/two.mp4"
            return motion_inspection(asset, profile=profile, mp4_url=mp4_url)

        with patch("apple_music_artwork.hls.require_parser"), \
                patch("apple_music_artwork.hls.inspect_motion", side_effect=inspect), \
                patch("apple_music_artwork.motion.prepare_motion", side_effect=motion_artifact) as download:
            code, result = self.run_cli("--all-motion", value=value, media_tools=self.tools)
        self.assertEqual(code, 0)
        self.assertEqual(download.call_count, 2)
        self.assertEqual(len({item["path"] for item in result["downloads"]}), 2)

    def test_distinct_masters_with_same_mp4_and_consistent_declarations_download_once(self):
        value = payload(motion=True)
        value["data"][0]["attributes"]["editorialVideo"]["motionSquareVideo1x1"] = {
            "video": "https://mvod.itunes.apple.com/itunes-assets/synthetic/another.m3u8"}

        def inspect(asset, http, profile):
            result = motion_inspection(asset, profile=profile)
            if asset.url != MOTION_URL:
                result = replace(result, layout=replace(result.layout, duration=2.0 + 1e-12))
            return result

        with patch("apple_music_artwork.hls.require_parser"), \
                patch("apple_music_artwork.hls.inspect_motion", side_effect=inspect) as inspection, \
                patch("apple_music_artwork.motion.prepare_motion", side_effect=motion_artifact) as download:
            code, result = self.run_cli("--all-motion", value=value, media_tools=self.tools)
        self.assertEqual(code, 0)
        self.assertEqual(inspection.call_count, 2)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(len(result["downloads"][0]["references"]), 2)
        self.assertEqual(len(result["downloads"][0]["motion_declarations"]), 2)

    def test_same_mp4_conflicting_declarations_fail_group_before_media_download(self):
        value = payload(motion=True)
        value["data"][0]["attributes"]["editorialVideo"]["motionSquareVideo1x1"] = {
            "video": "https://mvod.itunes.apple.com/itunes-assets/synthetic/another.m3u8"}
        for field, replacement in (("width", 1080), ("height", 1080), ("codecs", "avc1.640028"),
                                   ("frame_rate", 24.0), ("duration", 2.01)):
            def inspect(asset, http, profile):
                result = motion_inspection(asset, profile=profile)
                if asset.url != MOTION_URL:
                    if field == "duration":
                        return replace(result, layout=replace(result.layout, duration=replacement))
                    selected = replace(result.selected, **{field: replacement})
                    return replace(result, selected=selected, variants=(selected,))
                return result

            with self.subTest(field=field), patch("apple_music_artwork.hls.require_parser"), \
                    patch("apple_music_artwork.hls.inspect_motion", side_effect=inspect), \
                    patch("apple_music_artwork.motion.prepare_motion") as download:
                code, result = self.run_cli("--all-motion", value=value, media_tools=self.tools)
            self.assertEqual(code, 8)
            self.assertEqual(result["control"]["lifecycle"], "needs_action")
            self.assertEqual(result["errors"][0]["code"], "MOTION_DECLARATION_CONFLICT")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(len(result["downloads"][0]["references"]), 2)
            self.assertEqual(len(result["motion_inspections"]), 2)
            self.assertEqual(result["coverage"]["validation"]["validated_asset_ids"], [])
            download.assert_not_called()

    def test_inspection_failure_keeps_successful_static_download_and_failed_motion(self):
        failure = ArtworkError("HLS_HTTP_ERROR", "Synthetic playlist unavailable.", "download")
        with patch("apple_music_artwork.hls.require_parser"), \
                patch("apple_music_artwork.hls.inspect_motion", side_effect=failure):
            code, result = self.run_cli("--all-media", value=payload(motion=True), media_tools=self.tools, transport=ImageTransport())
        self.assertEqual(code, 8)
        self.assertEqual(result["control"]["lifecycle"], "needs_action")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(sorted(item["status"] for item in result["downloads"]), ["downloaded", "failed"])
        self.assertEqual(result["manifest_report"]["status"], "saved")
        self.assertEqual(result["coverage"]["inspection"]["motion"][0]["status"], "failed")

    def test_failed_motion_download_is_not_partial_just_because_inspection_succeeded(self):
        failure = ArtworkError("VIDEO_DECODE_FAILED", "Synthetic failed full decode.", "validation")
        with patch("apple_music_artwork.hls.require_parser"), \
                patch("apple_music_artwork.hls.inspect_motion", side_effect=motion_inspection), \
                patch("apple_music_artwork.motion.prepare_motion", side_effect=failure):
            code, result = self.run_cli("--all-motion", value=payload(motion=True), media_tools=self.tools)
        self.assertEqual(code, 8)
        self.assertEqual(result["control"]["lifecycle"], "needs_action")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["downloads"][0]["status"], "failed")

    def test_comparison_inspection_failure_preserves_other_storefront(self):
        def fetch(target):
            return payload(storefront=target.key.storefront, motion=True)

        def inspect(asset, http, profile):
            if asset.owner.storefront == "cn":
                raise ArtworkError("HLS_HTTP_ERROR", "Synthetic storefront inspection failure.", "download")
            return motion_inspection(asset, profile=profile)

        with patch("apple_music_artwork.hls.inspect_motion", side_effect=inspect), \
                patch("apple_music_artwork.motion.MediaTools.discover") as tools:
            code, result = self.run_cli("--list", "--inspect-motion", "--compare-storefronts", "cn", fetch=fetch,
                                        url=URL + "?l=en-US")
        self.assertEqual(code, 6)
        self.assertEqual(result["status"], "partial")
        self.assertEqual([item["status"] for item in result["comparison"]["results"]], ["success", "failed"])
        tools.assert_not_called()
