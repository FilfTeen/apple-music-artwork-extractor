"""Offline transport/authentication tests; tokens and catalog data are synthetic."""

import base64
from http.client import HTTPResponse
from io import BytesIO
import json
from unittest import TestCase
from unittest.mock import Mock
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from apple_music_artwork.client import AppleMusicClient, HttpClient, HttpResponse, UrllibTransport
from apple_music_artwork.errors import ArtworkError
from apple_music_artwork.urls import parse_url


class FakeTransport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def request(self, url, *, headers, timeout, max_bytes):
        self.requests.append((url, dict(headers), timeout, max_bytes))
        if not self.replies:
            raise AssertionError("Unexpected extra request")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def reply(status=200, body=b"", **headers):
    return HttpResponse(status, headers, body)


def token(label, *, kid="WebPlayKid", alg="ES256", expiry=2000):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    return encode({"kid": kid, "alg": alg}) + "." + encode({"exp": expiry, "label": label}) + ".c2ln"


PAGE = reply(body=b'<script src="/assets/app.js"></script>')
PAYLOAD = reply(body=b'{"data": [{"id": "1", "type": "albums", "attributes": {}}]}')
TARGET = parse_url("https://music.apple.com/us/album/example/1?l=en-US")


class HttpClientTests(TestCase):
    def test_strict_host_and_scheme_boundary_before_transport(self):
        forbidden = ["http://a.mzstatic.com/a.jpg", "https://a.mzstatic.com.evil.test/a.jpg",
                     "https://a.mzstatic.com@127.0.0.1/a.jpg", "https://127.0.0.1/a.jpg",
                     "https://a.mzstatic.com:444/a.jpg", "https://a.mzstatic.com/a.jpg#x",
                     "https://mzstatic.com/a.jpg", "https://a.mzstatic.com\\@evil.test/a.jpg"]
        for url in forbidden:
            with self.subTest(url=url):
                transport = FakeTransport()
                with self.assertRaises(ArtworkError) as failure:
                    HttpClient(transport).get(url, purpose="image")
                self.assertEqual(failure.exception.code, "UNSAFE_URL")
                self.assertFalse(transport.requests)

    def test_each_redirect_is_revalidated(self):
        transport = FakeTransport(reply(302, Location="https://127.0.0.1/private"))
        with self.assertRaises(ArtworkError) as failure:
            HttpClient(transport).get("https://music.apple.com/", purpose="page")
        self.assertEqual(failure.exception.code, "UNSAFE_URL")
        self.assertEqual(len(transport.requests), 1)

    def test_same_origin_redirect_preserves_auth(self):
        transport = FakeTransport(reply(302, Location="/next"), reply())
        HttpClient(transport).get("https://api.music.apple.com/first", purpose="catalog_api", headers={"Authorization": "Bearer synthetic"})
        self.assertEqual(transport.requests[1][0], "https://api.music.apple.com/next")
        self.assertEqual(transport.requests[1][1]["Authorization"], "Bearer synthetic")

    def test_allowed_image_cross_origin_redirect_never_leaks_auth(self):
        transport = FakeTransport(reply(302, Location="https://b.mzstatic.com/image.jpg"))
        with self.assertRaises(ArtworkError) as failure:
            HttpClient(transport).get("https://a.mzstatic.com/image.jpg", purpose="image", headers={"authorization": "Bearer synthetic"})
        self.assertEqual(failure.exception.code, "AUTH_REDIRECT_BLOCKED")
        self.assertEqual(len(transport.requests), 1)

    def test_transient_statuses_preserve_retry_after_within_deadline(self):
        transport = FakeTransport(reply(429, **{"Retry-After": "7"}), reply(503), reply(body=b"ok"))
        delays = []
        now = [0]
        def sleep(seconds):
            delays.append(seconds)
            now[0] += seconds
        result = HttpClient(transport, sleep=sleep, monotonic=lambda: now[0]).get("https://music.apple.com/", purpose="page")
        self.assertEqual(result.body, b"ok")
        self.assertEqual(delays, [7, 2])
        self.assertEqual(len(transport.requests), 3)

    def test_timeout_retries_are_bounded(self):
        transport = FakeTransport(TimeoutError("private detail"), URLError(TimeoutError()), TimeoutError())
        delays = []
        now = [0]
        def sleep(seconds):
            delays.append(seconds)
            now[0] += seconds
        with self.assertRaises(ArtworkError) as failure:
            HttpClient(transport, sleep=sleep, monotonic=lambda: now[0]).get("https://music.apple.com/", purpose="page")
        self.assertEqual(failure.exception.code, "NETWORK_ERROR")
        self.assertNotIn("private detail", str(failure.exception))
        self.assertEqual(delays, [1, 2])

    def test_permanent_http_status_does_not_retry(self):
        for status in (400, 401, 403, 404):
            transport = FakeTransport(reply(status))
            self.assertEqual(HttpClient(transport).get("https://music.apple.com/", purpose="page").status, status)
            self.assertEqual(len(transport.requests), 1)

    def test_request_and_redirect_budgets_are_bounded(self):
        transport = FakeTransport(*(reply(302, Location="/next") for _ in range(6)))
        with self.assertRaises(ArtworkError) as failure:
            HttpClient(transport).get("https://music.apple.com/", purpose="page")
        self.assertEqual(failure.exception.code, "REDIRECT_LIMIT")
        limited = HttpClient(FakeTransport(reply(503)), max_requests=1, sleep=lambda _: None)
        with self.assertRaises(ArtworkError) as failure:
            limited.get("https://music.apple.com/", purpose="page")
        self.assertEqual(failure.exception.code, "REQUEST_LIMIT")

    def test_real_urllib_read_limit_maps_image_error_to_download(self):
        class Response(BytesIO):
            status = 200
            headers = {"Content-Type": "image/jpeg"}
        transport = UrllibTransport()
        transport._opener = Mock()
        transport._opener.open.return_value = Response(b"123456789")
        with self.assertRaises(ArtworkError) as failure:
            HttpClient(transport).get("https://a.mzstatic.com/a.jpg", purpose="image", max_bytes=8)
        self.assertEqual((failure.exception.code, failure.exception.category), ("RESPONSE_TOO_LARGE", "download"))

    def test_truncated_chunked_response_is_a_purpose_specific_network_error(self):
        class SocketBytes:
            def makefile(self, mode):
                return BytesIO(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n10\r\nprivate-fragment")
        cases = [("image", "https://a.mzstatic.com/image.jpg", "download"),
                 ("catalog_api", "https://api.music.apple.com/v1/catalog/us/albums/1", "api")]
        for purpose, url, category in cases:
            with self.subTest(purpose=purpose):
                response = HTTPResponse(SocketBytes())
                response.begin()
                transport = UrllibTransport()
                transport._opener = Mock()
                transport._opener.open.return_value = response
                with self.assertRaises(ArtworkError) as failure:
                    HttpClient(transport, sleep=lambda _: None).get(url, purpose=purpose)
                self.assertEqual((failure.exception.code, failure.exception.category), ("NETWORK_ERROR", category))
                self.assertNotIn("private-fragment", str(failure.exception))
                self.assertEqual(transport._opener.open.call_count, 1)

    def test_response_repr_excludes_headers_and_body(self):
        representation = repr(reply(body=b"sensitive-body", Authorization="secret"))
        self.assertNotIn("secret", representation)
        self.assertNotIn("sensitive", representation)


class AppleMusicClientTests(TestCase):
    def http(self, transport):
        return HttpClient(transport, clock=lambda: 1000, sleep=lambda _: None)

    def test_catalog_requires_explicit_token(self):
        transport = FakeTransport()
        with self.assertRaises(ArtworkError) as failure:
            AppleMusicClient(self.http(transport), provider="catalog").fetch(TARGET)
        self.assertEqual(failure.exception.code, "TOKEN_REQUIRED")
        self.assertFalse(transport.requests)

    def test_catalog_uses_official_host_and_target_locale(self):
        transport = FakeTransport(PAYLOAD)
        AppleMusicClient(self.http(transport), provider="catalog", developer_token="synthetic.token").fetch(TARGET)
        endpoint, headers, _, _ = transport.requests[0]
        self.assertEqual(urlsplit(endpoint).hostname, "api.music.apple.com")
        self.assertEqual(parse_qs(urlsplit(endpoint).query), {"l": ["en-US"]})
        self.assertEqual(headers["Authorization"], "Bearer synthetic.token")

    def test_web_filters_candidates_and_validates_more_than_first(self):
        rejected, accepted = token("first"), token("second")
        screened = [token("expired", expiry=999), token("wrong-kid", kid="Other"), token("wrong-alg", alg="HS256")]
        bundle = reply(body=" ".join([*screened, rejected, accepted]).encode())
        transport = FakeTransport(PAGE, bundle, reply(401), PAYLOAD)
        client = AppleMusicClient(self.http(transport))
        self.assertIn("data", client.fetch(TARGET))
        auth_requests = [item for item in transport.requests if "Authorization" in item[1]]
        self.assertEqual([item[1]["Authorization"] for item in auth_requests], ["Bearer " + rejected, "Bearer " + accepted])
        query = parse_qs(urlsplit(auth_requests[0][0]).query)
        self.assertEqual(query["extend[albums]"], ["editorialArtwork,editorialVideo"])
        self.assertEqual(query["art[url]"], ["c,f"])
        self.assertNotIn(accepted, repr(client))
        self.assertNotIn(accepted, repr(client._token))

    def test_script_hosts_are_restricted_before_fetch(self):
        page = reply(body=b'<script src="https://evil.test/steal.js"></script><script src="/assets/app.js"></script>')
        transport = FakeTransport(page, reply(body=token("valid").encode()), PAYLOAD)
        AppleMusicClient(self.http(transport)).fetch(TARGET)
        self.assertTrue(all("evil.test" not in item[0] for item in transport.requests))

    def test_cached_401_refreshes_once(self):
        first, second = token("first"), token("second")
        transport = FakeTransport(PAGE, reply(body=first.encode()), PAYLOAD, reply(401), PAGE, reply(body=second.encode()), PAYLOAD)
        client = AppleMusicClient(self.http(transport))
        client.fetch(TARGET)
        client.fetch(TARGET)
        self.assertEqual(len(transport.requests), 7)
        self.assertEqual(transport.requests[-1][1]["Authorization"], "Bearer " + second)

    def test_401_after_refresh_fails_without_token_or_body_leak(self):
        value = token("never-accepted")
        transport = FakeTransport(PAGE, reply(body=value.encode()), reply(401, body=b"private body"), PAGE, reply(body=value.encode()), reply(401))
        with self.assertRaises(ArtworkError) as failure:
            AppleMusicClient(self.http(transport)).fetch(TARGET)
        self.assertEqual(failure.exception.code, "TOKEN_REJECTED")
        self.assertEqual(len(transport.requests), 6)
        self.assertNotIn(value, str(failure.exception.to_dict()))
        self.assertNotIn("private body", str(failure.exception.to_dict()))

    def test_invalid_json_and_schema_are_not_retried(self):
        for body, code in [(b'{"data":[],"data":[]}', "API_RESPONSE_INVALID"), (b'{"other":1}', "API_SCHEMA_CHANGED")]:
            transport = FakeTransport(reply(body=body))
            with self.assertRaises(ArtworkError) as failure:
                AppleMusicClient(self.http(transport), provider="catalog", developer_token="synthetic").fetch(TARGET)
            self.assertEqual(failure.exception.code, code)
            self.assertEqual(len(transport.requests), 1)


class FreshMetadataTests(TestCase):
    """Exercise the real urllib read loop with an independently closed socket boundary."""
    def setUp(self):
        from unittest.mock import patch
        for name in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
            guard = patch(name, side_effect=AssertionError('network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def wire(self, replies, **options):
        from email.message import Message
        class Response(BytesIO):
            def __init__(self, status, headers, body):
                super().__init__(body)
                self.status, self.headers, self.reads = status, Message(), 0
                for key, value in headers:
                    self.headers[key] = value
            def read1(self, size=-1):
                self.reads += 1
                return super().read(size)
        responses = [Response(*value) for value in replies]
        transport = UrllibTransport()
        transport._open = Mock(side_effect=responses)
        # Exercise serialization of an adapter without cloning to a real opener.
        transport.fork = None
        return HttpClient(transport, **options), responses, transport

    def test_before_read_rejects_media_missing_duplicate_and_ambiguous_mime(self):
        for headers in [[('Content-Type', 'video/mp4')], [],
                        [('Content-Type', 'text/plain'), ('Content-Type', 'video/mp4')],
                        [('Content-Type', 'text/plain, video/mp4')]]:
            parent, responses, _ = self.wire([(200, headers, b'media')])
            with self.assertRaises(ArtworkError) as raised:
                parent.fresh_metadata_view().get('https://mvod.itunes.apple.com/a.m3u8', purpose='hls')
            self.assertEqual(raised.exception.code, 'METADATA_BODY_BLOCKED')
            self.assertEqual(responses[0].reads, 0)
            self.assertTrue(responses[0].closed)

    def test_redirect_media_path_rejected_even_with_text_mime(self):
        parent, responses, _ = self.wire([(302, [('Location', '/a.mp4')], b''),
                                          (200, [('Content-Type', 'text/plain')], b'media')])
        with self.assertRaises(ArtworkError):
            parent.fresh_metadata_view().get('https://mvod.itunes.apple.com/a.m3u8', purpose='hls')
        self.assertEqual(responses[-1].reads, 0)

    def test_missing_capability_fails_before_io_no_real_fallback(self):
        transport = FakeTransport()
        with self.assertRaises(ArtworkError) as raised:
            HttpClient(transport).fresh_metadata_view().get('https://music.apple.com/', purpose='page')
        self.assertEqual(raised.exception.code, 'TRANSPORT_CAPABILITY_MISSING')
        self.assertFalse(transport.requests)

    def test_fresh_forks_bypass_cache_share_control_and_preserve_parent_cache(self):
        url = 'https://mvod.itunes.apple.com/a.m3u8'
        parent, responses, transport = self.wire([(200, [('Content-Type', 'text/plain')], b'old'),
            (200, [('Content-Type', 'text/plain')], b'new1'), (200, [('Content-Type', 'text/plain')], b'new2')])
        self.assertEqual(parent.get(url, purpose='hls').body, b'old')
        for expected in (b'new1', b'new2'):
            fresh = parent.fresh_metadata_view().fork()
            self.assertIs(fresh.control, parent.control)
            self.assertEqual(fresh.get(url, purpose='hls').body, expected)
            fresh.forget_response(url, purpose='hls')
        self.assertEqual(parent.get(url, purpose='hls').body, b'old')
        self.assertEqual(parent.request_count, 3)
        self.assertEqual(transport._open.call_count, 3)

    def test_fresh_control_budget_and_cancellation_not_reset(self):
        url = 'https://mvod.itunes.apple.com/a.m3u8'
        parent, _, transport = self.wire([(200, [('Content-Type', 'text/plain')], b'old')], max_requests=1)
        parent.get(url, purpose='hls')
        with self.assertRaises(ArtworkError) as raised:
            parent.fresh_metadata_view().get(url, purpose='hls')
        self.assertEqual(raised.exception.code, 'REQUEST_LIMIT')
        self.assertEqual(transport._open.call_count, 1)

    def test_metadata_view_rejects_media_get_and_stream_before_io(self):
        parent, _, transport = self.wire([])
        fresh = parent.fresh_metadata_view()
        with self.assertRaises(ArtworkError):
            fresh.get('https://a.mzstatic.com/a.jpg', purpose='image')
        with self.assertRaises(ArtworkError):
            fresh.stream_to('https://mvod.itunes.apple.com/a.mp4', BytesIO(), expected_bytes=2)
        transport._open.assert_not_called()

    def test_encoded_media_path_cannot_hide_behind_playlist_mime(self):
        parent, responses, _ = self.wire([(200, [('Content-Type', 'application/vnd.apple.mpegurl')], b'media')])
        with self.assertRaises(ArtworkError):
            parent.fresh_metadata_view().get('https://mvod.itunes.apple.com/a%2emp4', purpose='hls')
        self.assertEqual(responses[0].reads, 0)

    def test_cancelled_control_is_shared_by_fresh_fork(self):
        parent, _, transport = self.wire([])
        fresh = parent.fresh_metadata_view().fork()
        parent.control.cancel()
        with self.assertRaises(ArtworkError) as raised:
            fresh.get('https://music.apple.com/', purpose='page')
        self.assertEqual(raised.exception.code, 'CANCELLED')
        transport._open.assert_not_called()

    def test_fresh_retry_respects_original_cooldown_and_request_window(self):
        now, sleeps = [0], []
        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
        parent, _, transport = self.wire([(429, [('Retry-After', '2')], b''),
            (200, [('Content-Type', 'text/html')], b'ok')], monotonic=lambda: now[0], sleep=sleep)
        parent.control.cooldown(('https', 'music.apple.com', 443), 3)
        fresh = parent.fresh_metadata_view()
        before = parent.control.budget_snapshot()
        self.assertEqual(fresh.get('https://music.apple.com/', purpose='page').body, b'ok')
        self.assertEqual(sleeps, [3, 2])
        self.assertEqual(parent.request_count, 2)
        self.assertEqual(before['window_id'], parent.control.budget_snapshot()['window_id'])

    def test_real_guarded_read_loop_accepts_only_purpose_specific_metadata(self):
        for purpose, url, mime in [('page', 'https://music.apple.com/', 'text/html'),
                ('bundle', 'https://music.apple.com/assets/app.js', 'application/javascript'),
                ('catalog_api', 'https://api.music.apple.com/v1/catalog/us/albums/1', 'application/json'),
                ('web_api', 'https://amp-api.music.apple.com/v1/catalog/us/albums/1', 'application/json'),
                ('hls', 'https://mvod.itunes.apple.com/playlist', 'application/vnd.apple.mpegurl')]:
            with self.subTest(purpose=purpose):
                parent, responses, _ = self.wire([(200, [('Content-Type', mime)], b'metadata')])
                self.assertEqual(parent.fresh_metadata_view().get(url, purpose=purpose).body, b'metadata')
                self.assertGreater(responses[0].reads, 0)
                self.assertEqual(parent.control.snapshot()['received_bytes'], 8)
        parent, responses, _ = self.wire([(200, [('Content-Type', 'text/plain')], b'unknown')])
        with self.assertRaises(ArtworkError):
            parent.fresh_metadata_view().get('https://mvod.itunes.apple.com/playlist', purpose='hls')
        self.assertEqual(responses[0].reads, 0)

    def test_real_head_allows_video_headers_without_reading_body(self):
        parent, responses, _ = self.wire([(200, [('Content-Type', 'video/mp4'), ('Content-Length', '5')], b'media')])
        response = parent.fresh_metadata_view().head('https://mvod.itunes.apple.com/a.mp4', purpose='motion')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b'')
        self.assertEqual(responses[0].reads, 0)
