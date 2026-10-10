"""Synthetic streaming and HEAD adapters; no media or network dependencies."""

from hashlib import sha256
from http.client import IncompleteRead
from io import BytesIO
from unittest import TestCase

from apple_music_artwork.client import HttpClient, HttpResponse
from apple_music_artwork.errors import ArtworkError


MEDIA_URL = "https://mvod.itunes.apple.com/synthetic/movie.mp4"


class StreamResponse:
    def __init__(self, body=b"0123456789", *, status=200, headers=None, fail_after=None):
        self.body = BytesIO(body)
        self.status = status
        self.headers = {"Content-Length": str(len(body)), "Content-Type": "video/mp4"} if headers is None else headers
        self.closed = False
        self.read_sizes = []
        self.fail_after = fail_after

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def read(self, size):
        self.read_sizes.append(size)
        if self.fail_after is not None and self.body.tell() >= self.fail_after:
            raise IncompleteRead(b"private body detail")
        return self.body.read(min(size, 4))


class MediaTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def _next(self, method, url, headers):
        self.requests.append((method, url, headers))
        reply = self.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def head(self, url, **kwargs):
        return self._next("HEAD", url, kwargs["headers"])

    def stream(self, url, **kwargs):
        return self._next("STREAM", url, kwargs["headers"])


class MediaClientTests(TestCase):
    def test_head_preserves_method_through_redirect_and_resolves_actual_url(self):
        transport = MediaTransport(HttpResponse(302, {"Location": "next.mp4"}, b""), HttpResponse(200, {"Content-Length": "10"}, b""))
        response = HttpClient(transport).head(MEDIA_URL, purpose="motion")
        self.assertEqual([item[0] for item in transport.requests], ["HEAD", "HEAD"])
        self.assertEqual(response.final_url, "https://mvod.itunes.apple.com/synthetic/next.mp4")
        self.assertEqual(response.body, b"")

    def test_missing_injected_capabilities_do_not_fall_back_to_network(self):
        client = HttpClient(object())
        for operation in (lambda: client.head(MEDIA_URL, purpose="motion"),
                          lambda: client.stream_to(MEDIA_URL, BytesIO(), expected_bytes=10)):
            with self.assertRaises(ArtworkError) as failure:
                operation()
            self.assertEqual(failure.exception.code, "TRANSPORT_CAPABILITY_MISSING")
        self.assertEqual(client.request_count, 0)

    def test_stream_is_chunked_hashed_and_closed(self):
        response = StreamResponse()
        sink = BytesIO()
        client = HttpClient(MediaTransport(response))
        result = client.stream_to(MEDIA_URL, sink, expected_bytes=10)
        self.assertEqual(sink.getvalue(), b"0123456789")
        self.assertEqual(result["sha256"], sha256(sink.getvalue()).hexdigest())
        self.assertEqual(result["bytes"], 10)
        self.assertTrue(response.closed)
        self.assertGreater(len(response.read_sizes), 2)
        self.assertLessEqual(max(response.read_sizes), 256 * 1024)

    def test_stream_redirects_are_checked_before_reading(self):
        for destination, code in (("https://mvod.itunes.apple.com.evil.test/a.mp4", "UNSAFE_URL"),
                                  ("https://a.mvod.itunes.apple.com/a.mp4", "AUTH_REDIRECT_BLOCKED")):
            response = StreamResponse(status=302, headers={"Location": destination})
            with self.assertRaises(ArtworkError) as failure:
                HttpClient(MediaTransport(response)).stream_to(MEDIA_URL, BytesIO(), expected_bytes=10,
                                                               headers={"Authorization": "synthetic"})
            self.assertEqual(failure.exception.code, code)
            self.assertEqual(response.read_sizes, [])
            self.assertTrue(response.closed)

    def test_stream_retries_only_before_body_and_uses_shared_request_budget(self):
        first, second = StreamResponse(status=503), StreamResponse()
        delays = []
        transport = MediaTransport(first, second)
        now = [0]
        def sleep(seconds):
            delays.append(seconds)
            now[0] += seconds
        client = HttpClient(transport, sleep=sleep, monotonic=lambda: now[0], max_requests=2)
        client.stream_to(MEDIA_URL, BytesIO(), expected_bytes=10)
        self.assertEqual(delays, [1])
        self.assertTrue(first.closed and second.closed)
        self.assertEqual(client.request_count, 2)
        with self.assertRaises(ArtworkError) as failure:
            client.head(MEDIA_URL, purpose="motion")
        self.assertEqual(failure.exception.code, "REQUEST_LIMIT")

    def test_partial_protocol_error_is_not_retried_and_is_redacted(self):
        response = StreamResponse(fail_after=4)
        transport = MediaTransport(response)
        sink = BytesIO()
        with self.assertRaises(ArtworkError) as failure:
            HttpClient(transport).stream_to(MEDIA_URL, sink, expected_bytes=10)
        self.assertEqual(failure.exception.code, "NETWORK_ERROR")
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(sink.getvalue(), b"0123")
        self.assertTrue(response.closed)
        self.assertNotIn("private", str(failure.exception))

    def test_length_encoding_content_type_and_preflight_limits(self):
        cases = [StreamResponse(b"0123", headers={"Content-Type": "video/mp4"}),
                 StreamResponse(b"01234567890", headers={"Content-Type": "video/mp4"}),
                 StreamResponse(headers={"Content-Length": "9", "Content-Type": "video/mp4"}),
                 StreamResponse(headers={"Content-Encoding": "gzip", "Content-Type": "video/mp4"}),
                 StreamResponse(headers={"Content-Type": "text/html"})]
        for response in cases:
            with self.subTest(headers=response.headers), self.assertRaises(ArtworkError):
                HttpClient(MediaTransport(response)).stream_to(MEDIA_URL, BytesIO(), expected_bytes=10)
            self.assertTrue(response.closed)
        client = HttpClient(MediaTransport())
        with self.assertRaises(ArtworkError) as failure:
            client.stream_to(MEDIA_URL, BytesIO(), expected_bytes=11, max_bytes=10)
        self.assertEqual(failure.exception.code, "RESPONSE_TOO_LARGE")
        self.assertEqual(client.request_count, 0)

    def test_malformed_content_length_is_bounded_ascii_and_never_retried(self):
        for value in ("9" * 4301, "²", "١٠", "10.0", "-10"):
            with self.subTest(value=value[:16]):
                response = StreamResponse(headers={"Content-Length": value, "Content-Type": "video/mp4"})
                transport = MediaTransport(response)
                with self.assertRaises(ArtworkError) as failure:
                    HttpClient(transport).stream_to(MEDIA_URL, BytesIO(), expected_bytes=10)
                self.assertEqual(failure.exception.code, "DOWNLOAD_LENGTH_MISMATCH")
                self.assertFalse(failure.exception.retryable)
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(response.read_sizes, [])
                self.assertTrue(response.closed)

    def test_deadline_checks_time_spent_inside_last_read(self):
        now = [0]
        class SlowResponse(StreamResponse):
            def read(self, size):
                result = super().read(size)
                now[0] = 2
                return result
        response = SlowResponse()
        with self.assertRaises(ArtworkError) as failure:
            HttpClient(MediaTransport(response), monotonic=lambda: now[0]).stream_to(
                MEDIA_URL, BytesIO(), expected_bytes=10, max_duration=1)
        self.assertEqual(failure.exception.code, "NETWORK_TIMEOUT")
        self.assertTrue(response.closed)

    def test_short_sink_write_is_a_download_failure(self):
        class Sink:
            def write(self, body):
                return len(body) - 1
        with self.assertRaises(ArtworkError) as failure:
            HttpClient(MediaTransport(StreamResponse())).stream_to(MEDIA_URL, Sink(), expected_bytes=10)
        self.assertEqual(failure.exception.code, "OUTPUT_WRITE_FAILED")
