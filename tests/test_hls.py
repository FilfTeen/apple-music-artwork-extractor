"""Small synthetic HLS examples exercise the real text parser without HTTP."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from threading import Barrier
from unittest import TestCase
from unittest.mock import patch

from apple_music_artwork.client import HttpClient, HttpResponse
from apple_music_artwork.errors import ArtworkError
from apple_music_artwork.hls import inspect_motion, require_parser
from apple_music_artwork.models import ResourceKey, VisualAsset


MASTER_URL = "https://mvod.itunes.apple.com/synthetic/master.m3u8"
ASSET = VisualAsset(ResourceKey("us", "albums", "1"), "editorialVideo.motionSquare", "video", "motion",
                    MASTER_URL, "synthetic-hls")


def variant(uri="video.m3u8", resolution="1920x1080", codec="hvc1.2.4.L120.B0", fps=30, bandwidth=2000000, average=1500000):
    return f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},AVERAGE-BANDWIDTH={average},RESOLUTION={resolution},FRAME-RATE={fps},CODECS="{codec}"\n{uri}\n'


MASTER = "#EXTM3U\n" + variant()
MEDIA = '''#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:1
#EXT-X-MAP:URI="movie.mp4",BYTERANGE="4@0"
#EXTINF:1.0,
#EXT-X-BYTERANGE:3@4
movie.mp4
#EXTINF:1.0,
#EXT-X-BYTERANGE:3
movie.mp4
#EXT-X-ENDLIST
'''


class HlsTransport:
    def __init__(self, master=MASTER, media=MEDIA, *, head_length=10, redirect=False):
        self.texts = [master, media]
        self.requests = []
        self.head_length = head_length
        self.redirect = redirect

    def request(self, url, **kwargs):
        self.requests.append(("GET", url))
        if self.redirect:
            self.redirect = False
            return HttpResponse(302, {"Location": "/relocated/master.m3u8?token=synthetic"}, b"")
        return HttpResponse(200, {"Content-Type": "application/vnd.apple.mpegurl"}, self.texts.pop(0).encode())

    def head(self, url, **kwargs):
        self.requests.append(("HEAD", url))
        return HttpResponse(200, {"Content-Length": str(self.head_length), "Content-Type": "video/mp4"}, b"")


class HlsTests(TestCase):
    def inspect(self, master=MASTER, media=MEDIA, **options):
        transport = HlsTransport(master, media, **options)
        return inspect_motion(ASSET, HttpClient(transport)), transport

    def test_supported_ended_contiguous_whole_file_and_implicit_range(self):
        result, transport = self.inspect()
        self.assertTrue(result.supported)
        self.assertEqual(result.layout.ranges, ((0, 4), (4, 3), (7, 3)))
        self.assertEqual(result.layout.duration, 2)
        self.assertEqual(result.request_key, ("https://mvod.itunes.apple.com/synthetic/movie.mp4", 10))
        self.assertEqual([method for method, _ in transport.requests], ["GET", "GET", "HEAD"])
        self.assertFalse(result.to_dict()["media_body_checked"])

    def test_resolution_then_fps_then_codec_then_same_codec_bitrate(self):
        choices = [variant("avc.m3u8", codec="avc1.640028", bandwidth=9000000),
                   variant("high.m3u8", resolution="2160x2160", fps=24),
                   variant("hevc-low.m3u8", resolution="2160x2160"),
                   variant("hevc.m3u8", resolution="2160x2160", average=3000000, bandwidth=4000000)]
        result, _ = self.inspect("#EXTM3U\n" + "".join(reversed(choices)))
        self.assertTrue(result.supported)
        self.assertTrue(result.selected.url.endswith("hevc.m3u8"))
        self.assertEqual(len(result.variants), 4)

    def test_compatible_selects_h264_and_iframe_is_excluded(self):
        master = "#EXTM3U\n" + variant("hevc.m3u8", resolution="2160x2160") + variant("h264.m3u8", codec="avc1.640028")
        master += '#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=9999999,RESOLUTION=8000x8000,CODECS="hvc1.2.4.L120.B0",URI="iframe.m3u8"\n'
        transport = HlsTransport(master)
        result = inspect_motion(ASSET, HttpClient(transport), profile="compatible")
        self.assertTrue(result.supported)
        self.assertEqual(result.selected.codec, "h264")
        self.assertEqual(len(result.variants), 2)
        self.assertEqual(result.iframe_variant_count, 1)
        self.assertFalse(any("iframe" in url for _, url in transport.requests))

    def test_master_redirect_and_relative_uri_use_actual_base(self):
        result, transport = self.inspect(redirect=True)
        self.assertTrue(result.supported)
        self.assertEqual(transport.requests[2][1], "https://mvod.itunes.apple.com/relocated/video.m3u8")
        self.assertEqual(result.layout.mp4_url, "https://mvod.itunes.apple.com/relocated/movie.mp4")

    def test_query_only_and_parent_uri_resolution(self):
        master = "#EXTM3U\n" + variant("?variant=1")
        media = MEDIA.replace("movie.mp4", "../media/movie.mp4?raw=1")
        result, transport = self.inspect(master, media)
        self.assertTrue(result.supported)
        self.assertEqual(transport.requests[1][1], MASTER_URL + "?variant=1")
        self.assertEqual(result.layout.mp4_url, "https://mvod.itunes.apple.com/media/movie.mp4?raw=1")

    def test_unsupported_layouts_never_request_mp4_body_or_keys(self):
        examples = [MEDIA.replace("#EXT-X-ENDLIST", ""),
                    MEDIA.replace("#EXT-X-VERSION:7", '#EXT-X-VERSION:7\n#EXT-X-KEY:METHOD=AES-128,URI="https://private.invalid/key"'),
                    MEDIA.replace("3@4", "3@5"), MEDIA.replace("3@4", "3@3"),
                    MEDIA.replace("3@4", "3"),
                    MEDIA.replace("movie.mp4\n#EXTINF", "other.mp4\n#EXTINF"),
                    MEDIA.replace("#EXT-X-BYTERANGE:3\n", ""),
                    MEDIA.replace("#EXTINF:1.0,", "#EXT-X-DISCONTINUITY\n#EXTINF:1.0,", 1),
                    MEDIA.replace("4@0", "4@2"), MEDIA.replace("3@4", "268435456@4")]
        for media in examples:
            with self.subTest(media=media):
                result, transport = self.inspect(media=media)
                self.assertFalse(result.supported)
                self.assertEqual(result.status, "unsupported")
                self.assertIsNone(result.request_key)
                self.assertEqual([method for method, _ in transport.requests], ["GET", "GET"])

    def test_head_mismatch_is_unsupported_after_metadata_only_request(self):
        result, transport = self.inspect(head_length=11)
        self.assertFalse(result.supported)
        self.assertEqual(result.error.code, "UNSUPPORTED_HLS_LAYOUT")
        self.assertEqual(transport.requests[-1][0], "HEAD")

    def test_master_encryption_missing_metadata_and_unknown_codec_are_visible(self):
        examples = [MASTER.replace("#EXTM3U", '#EXTM3U\n#EXT-X-SESSION-KEY:METHOD=AES-128,URI="key"'),
                    MASTER.replace("FRAME-RATE=30,", ""), MASTER.replace("hvc1.2.4.L120.B0", "vp09.00.10.08")]
        for master in examples:
            result, transport = self.inspect(master)
            self.assertFalse(result.supported)
            self.assertTrue(result.variants)
            self.assertEqual(len(transport.requests), 1)

    def test_unavailable_profile_and_parser_dependency_are_explicit(self):
        result = inspect_motion(ASSET, HttpClient(HlsTransport()), profile="compatible")
        self.assertEqual(result.error.code, "HLS_PROFILE_UNAVAILABLE")
        with patch("apple_music_artwork.hls.importlib.import_module", side_effect=ImportError):
            with self.assertRaises(ArtworkError) as failure:
                require_parser()
        self.assertEqual(failure.exception.code, "MOTION_DEPENDENCY_MISSING")

    def test_external_uri_and_oversized_integer_fail_without_followup(self):
        with self.assertRaises(ArtworkError) as failure:
            self.inspect("#EXTM3U\n" + variant("https://evil.invalid/video.m3u8"))
        self.assertEqual(failure.exception.code, "UNSAFE_URL")
        result, transport = self.inspect(media=MEDIA.replace("3@4", "9" * 5000 + "@4"))
        self.assertEqual(result.error.code, "RESPONSE_TOO_LARGE")
        self.assertEqual(len(transport.requests), 2)

    def test_aliases_keep_individual_inspection_identity_and_same_request_key(self):
        first, _ = self.inspect()
        alias = replace(ASSET, semantic_path="editorialVideo.motionDetailSquare")
        second = inspect_motion(alias, HttpClient(HlsTransport()))
        self.assertNotEqual(first.asset_id, second.asset_id)
        self.assertEqual(first.request_key, second.request_key)

    def test_duplicate_attributes_cannot_hide_encryption_or_replace_range(self):
        examples = [MEDIA.replace("#EXT-X-MAP:", '#EXT-X-KEY:METHOD=AES-128,METHOD=NONE,URI="key.bin"\n#EXT-X-MAP:'),
                    MEDIA.replace('BYTERANGE="4@0"', 'BYTERANGE="5@0",BYTERANGE="4@0"')]
        for media in examples:
            result, transport = self.inspect(media=media)
            self.assertFalse(result.supported)
            self.assertEqual(result.error.code, "HLS_INVALID")
            self.assertFalse(any(method == "HEAD" for method, _ in transport.requests))

    def test_delta_and_partial_playlist_declarations_are_unsupported(self):
        tags = ['#EXT-X-SKIP:SKIPPED-SEGMENTS=5',
                '#EXT-X-PRELOAD-HINT:TYPE=PART,URI="later.mp4",BYTERANGE-START=20',
                '#EXT-X-PART-INF:PART-TARGET=0.5']
        for tag in tags:
            with self.subTest(tag=tag):
                result, transport = self.inspect(media=MEDIA.replace("#EXT-X-ENDLIST", tag + "\n#EXT-X-ENDLIST"))
                self.assertFalse(result.supported)
                self.assertFalse(any(method == "HEAD" for method, _ in transport.requests))

    def test_overflowing_total_duration_is_a_structured_invalid_playlist(self):
        result, transport = self.inspect(media=MEDIA.replace("#EXTINF:1.0", "#EXTINF:1e308"))
        self.assertFalse(result.supported)
        self.assertEqual(result.error.code, "HLS_INVALID")
        self.assertFalse(any(method == "HEAD" for method, _ in transport.requests))

    def test_observed_apple_private_bitrate_attribute_is_ignored_for_quality(self):
        # Apple uses this private name on ordinary and I-frame variants. Its
        # value is not the standard AVERAGE-BANDWIDTH declaration.
        master = MASTER.replace("BANDWIDTH=2000000,", "BANDWIDTH=2000000,_AVG-BANDWIDTH=999999999,")
        master += '#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=100,_AVG-BANDWIDTH=99,URI="iframe.m3u8"\n'
        result, _ = self.inspect(master)
        self.assertTrue(result.supported)
        self.assertEqual(result.selected.average_bandwidth, 1500000)
        self.assertEqual(result.selected.bandwidth, 2000000)
        self.assertEqual(result.iframe_variant_count, 1)

    def test_private_bitrate_exception_does_not_allow_duplicates_or_key_attributes(self):
        master = MASTER.replace("BANDWIDTH=2000000,", "BANDWIDTH=2000000,_AVG-BANDWIDTH=1,_AVG-BANDWIDTH=2,")
        result, transport = self.inspect(master)
        self.assertEqual(result.error.code, "HLS_INVALID")
        self.assertEqual(len(transport.requests), 1)
        media = MEDIA.replace("#EXT-X-MAP:", "#EXT-X-KEY:METHOD=NONE,_AVG-BANDWIDTH=1\n#EXT-X-MAP:")
        result, transport = self.inspect(media=media)
        self.assertEqual(result.error.code, "HLS_INVALID")
        self.assertFalse(any(method == "HEAD" for method, _ in transport.requests))

    def test_successful_alias_inspection_reuses_bounded_hls_and_head(self):
        transport = HlsTransport()
        client = HttpClient(transport)
        first = inspect_motion(ASSET, client)
        second = inspect_motion(replace(ASSET, semantic_path="editorialVideo.alias"), client)
        self.assertTrue(first.supported and second.supported)
        self.assertNotEqual(first.asset_id, second.asset_id)
        self.assertEqual(first.request_key, second.request_key)
        self.assertEqual([method for method, _ in transport.requests], ["GET", "GET", "HEAD"])
        self.assertEqual(client.control.snapshot()["cache_hits"], 3)

    def test_http200_invalid_or_unsupported_hls_is_retrieved_again(self):
        for body in ("<html>not HLS</html>", MASTER.replace("hvc1.2.4.L120.B0", "vp09.00.10.08")):
            with self.subTest(body=body):
                transport = HlsTransport(body, body)
                client = HttpClient(transport)
                self.assertFalse(inspect_motion(ASSET, client).supported)
                self.assertFalse(inspect_motion(ASSET, client).supported)
                self.assertEqual(len(transport.requests), 2)
                self.assertEqual(client.control.snapshot()["cache_hits"], 0)

    def test_http200_failed_head_constraint_does_not_seed_cache(self):
        transport = HlsTransport(head_length=11)
        transport.texts.extend([MASTER, MEDIA])
        client = HttpClient(transport)
        self.assertFalse(inspect_motion(ASSET, client).supported)
        transport.head_length = 10
        self.assertTrue(inspect_motion(ASSET, client).supported)
        self.assertEqual([method for method, _ in transport.requests], ["GET", "GET", "HEAD", "GET", "GET", "HEAD"])
        self.assertEqual(client.control.snapshot()["cache_hits"], 0)

    def test_parallel_invalid_hls_calls_do_not_publish_a_failed_cache_entry(self):
        ready = Barrier(2)
        calls = []
        class Transport:
            def request(self, url, **options):
                calls.append(url)
                return HttpResponse(200, {}, b"<html>not a playlist</html>")
        client = HttpClient(Transport())
        def inspect(index):
            ready.wait(timeout=2)
            return inspect_motion(ASSET, client.fork(item_id=str(index)))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(inspect, range(2)))
        self.assertTrue(all(not result.supported for result in results))
        self.assertFalse(inspect_motion(ASSET, client).supported)
        self.assertEqual(len(calls), 3)
        self.assertEqual(client.control.snapshot()["cache_hits"], 0)

    def test_media_request_failure_keeps_selection_and_resume_never_refetches_master(self):
        class FailingMedia(HlsTransport):
            def request(self, url, **options):
                if url.endswith("video.m3u8"):
                    self.requests.append(("GET", url))
                    return HttpResponse(404, {}, b"")
                return super().request(url, **options)
        first = FailingMedia()
        with self.assertRaises(ArtworkError) as failure:
            inspect_motion(ASSET, HttpClient(first))
        previous = failure.exception.inspection
        self.assertEqual(previous.status, "failed")
        self.assertFalse(previous.supported)
        self.assertIsNotNone(previous.selected)
        self.assertIsNone(previous.provisional_layout)
        frozen = previous.to_dict()
        later = HlsTransport(master=MEDIA)
        resumed = inspect_motion(ASSET, HttpClient(later), previous=frozen)
        self.assertTrue(resumed.supported)
        self.assertEqual(resumed.selected, previous.selected)
        self.assertEqual(later.requests, [("GET", previous.selected.url), ("HEAD", resumed.layout.source_mp4_url)])
        self.assertEqual(frozen, previous.to_dict())

    def _head_failure(self):
        class FailingHead(HlsTransport):
            def head(self, url, **options):
                self.requests.append(("HEAD", url))
                return HttpResponse(404, {}, b"")
        with self.assertRaises(ArtworkError) as failure:
            inspect_motion(ASSET, HttpClient(FailingHead()))
        return failure.exception.inspection

    def test_head_failure_keeps_unverified_layout_and_resume_only_requests_original_head(self):
        previous = self._head_failure()
        self.assertEqual(previous.provisional_layout.ranges, ((0, 4), (4, 3), (7, 3)))
        self.assertIsNone(previous.layout)
        self.assertIsNone(previous.request_key)
        self.assertFalse(previous.to_dict()["provisional_layout"]["head_verified"])
        for saved in (previous, previous.to_dict()):
            later = HlsTransport()
            resumed = inspect_motion(ASSET, HttpClient(later), previous=saved)
            self.assertTrue(resumed.supported)
            self.assertIsNone(resumed.provisional_layout)
            self.assertEqual(later.requests, [("HEAD", previous.provisional_layout.source_mp4_url)])

    def test_saved_metadata_error_context_is_admitted_and_preserved(self):
        from apple_music_artwork.hls import _previous_inspection
        previous = self._head_failure().to_dict()
        previous["error"]["context"] = {"reason": "server_cooldown", "retry_at": 1600}
        restored = _previous_inspection(previous, ASSET, "best")
        self.assertEqual(dict(restored.error.context), previous["error"]["context"])
        previous["error"]["context"] = {"raw_response": "private"}
        later = HlsTransport()
        with self.assertRaises(ArtworkError) as failure:
            inspect_motion(ASSET, HttpClient(later), previous=previous)
        self.assertEqual(failure.exception.code, "RUN_STATE_INVALID")
        self.assertEqual(later.requests, [])

    def test_frozen_provisional_length_change_is_unsupported_without_reselection(self):
        previous = self._head_failure()
        later = HlsTransport(head_length=11)
        result = inspect_motion(ASSET, HttpClient(later), previous=previous.to_dict())
        self.assertFalse(result.supported)
        self.assertEqual(result.error.code, "UNSUPPORTED_HLS_LAYOUT")
        self.assertEqual(later.requests, [("HEAD", previous.provisional_layout.source_mp4_url)])
        never = HlsTransport()
        with self.assertRaises(ArtworkError) as failure:
            inspect_motion(ASSET, HttpClient(never), previous=result.to_dict())
        self.assertEqual(failure.exception.code, "RUN_STATE_INVALID")
        self.assertEqual(never.requests, [])

    def test_partial_identity_selection_and_range_tampering_fail_before_network(self):
        previous = self._head_failure().to_dict()
        cases = []
        for name, value in (("asset_id", "different"), ("master_url", MASTER_URL + "?changed=1"),
                            ("profile", "compatible"), ("status", "supported")):
            changed = deepcopy(previous)
            changed[name] = value
            cases.append(changed)
        changed = deepcopy(previous)
        changed["selected"]["width"] = 1
        cases.append(changed)
        changed = deepcopy(previous)
        changed["provisional_layout"]["ranges"][1]["offset"] = 3
        cases.append(changed)
        changed = deepcopy(previous)
        changed["provisional_layout"]["head_verified"] = True
        cases.append(changed)
        changed = deepcopy(previous)
        changed["error"]["code"] = "HLS_INVALID"
        cases.append(changed)
        for saved in cases:
            later = HlsTransport()
            with self.subTest(saved=saved), self.assertRaises(ArtworkError) as failure:
                inspect_motion(ASSET, HttpClient(later), previous=saved)
            self.assertEqual(failure.exception.code, "RUN_STATE_INVALID")
            self.assertEqual(later.requests, [])

    def test_failed_first_master_can_complete_first_inspection_of_that_snapshot(self):
        class FailedMaster(HlsTransport):
            def request(self, url, **options):
                self.requests.append(("GET", url))
                return HttpResponse(404, {}, b"")
        with self.assertRaises(ArtworkError) as failure:
            inspect_motion(ASSET, HttpClient(FailedMaster()))
        previous = failure.exception.inspection
        self.assertIsNone(previous.selected)
        later = HlsTransport()
        self.assertTrue(inspect_motion(ASSET, HttpClient(later), previous=previous.to_dict()).supported)
        self.assertEqual(later.requests[0], ("GET", MASTER_URL))
        self.assertEqual(len(later.requests), 3)
