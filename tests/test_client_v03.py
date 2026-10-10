"""Offline request deadlines, worker retries, cache contexts and read seams."""

from datetime import datetime, timezone
from email.utils import format_datetime
from io import BytesIO
from unittest import TestCase
from unittest.mock import Mock

from apple_music_artwork.client import HttpClient, HttpResponse, UrllibTransport
from apple_music_artwork.errors import ArtworkError
from test_client import FakeTransport, reply
from test_client_media import MEDIA_URL, MediaTransport, StreamResponse


class Clock:
    def __init__(self):
        self.now = 0.0
        self.delays = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += seconds


class RequestV03Tests(TestCase):
    def test_retry_after_declaration_is_not_shortened_by_clock_sampling(self):
        ticks = [0.0]
        def monotonic():
            ticks[0] += 0.000001
            return ticks[0]
        client = HttpClient(FakeTransport(reply(429, **{"Retry-After": "600"})),
                            monotonic=monotonic, clock=lambda: 1000)
        with self.assertRaises(ArtworkError) as failure:
            client.get("https://a.mzstatic.com/a.png", purpose="image")
        self.assertEqual(failure.exception.code, "RETRY_AFTER_DEADLINE")
        self.assertEqual(failure.exception.retry_after_seconds, 600)
        self.assertEqual(failure.exception.context["retry_at"], 1600)

    def test_recovery_respects_a_longer_cooldown_already_observed_by_another_worker(self):
        timer = Clock()
        client = HttpClient(FakeTransport(), monotonic=timer.monotonic, clock=lambda: 1000)
        client.control.cooldown(("https", "a.mzstatic.com", 443), 600)
        with self.assertRaises(ArtworkError) as failure:
            client._retry("https://a.mzstatic.com/a.png", {}, 1, 1, 60, "download")
        self.assertEqual(failure.exception.code, "RETRY_AFTER_DEADLINE")
        self.assertEqual(failure.exception.context["retry_at"], 1600)

    def test_exhausted_http_and_stream_attempts_keep_safe_earliest_retry_time(self):
        timer = Clock()
        for stream in (False, True):
            response = (StreamResponse(status=429, headers={"Retry-After": "7"}) if stream else
                        reply(429, **{"Retry-After": "7", "Authorization": "private"}))
            transport = MediaTransport(response) if stream else FakeTransport(response)
            client = HttpClient(transport, sleep=timer.sleep, monotonic=timer.monotonic, clock=lambda: 1000)
            with self.subTest(stream=stream), self.assertRaises(ArtworkError) as failure:
                if stream:
                    client.stream_to(MEDIA_URL, BytesIO(), expected_bytes=10, max_attempts=1)
                else:
                    client.get("https://a.mzstatic.com/a.png", purpose="image", max_attempts=1)
            self.assertEqual(failure.exception.context.get("retry_at"), 1007)
            self.assertEqual(failure.exception.context.get("http_status"), 429)
            self.assertEqual(failure.exception.context.get("reason"), "server_cooldown")
            self.assertNotIn("private", str(failure.exception.to_dict()))

    def test_retry_after_beyond_deadline_stops_without_shortening_or_sleep(self):
        timer = Clock()
        transport = FakeTransport(reply(429, **{"Retry-After": "600"}))
        client = HttpClient(transport, sleep=timer.sleep, monotonic=timer.monotonic, clock=lambda: 1000)
        with self.assertRaises(ArtworkError) as failure:
            client.get("https://music.apple.com/", purpose="page")
        self.assertEqual(failure.exception.code, "RETRY_AFTER_DEADLINE")
        self.assertFalse(failure.exception.retryable)
        self.assertEqual(failure.exception.to_dict()["retry_after_seconds"], 600)
        self.assertEqual(failure.exception.to_dict()["retry_at"], 1600)
        self.assertEqual(timer.delays, [])
        self.assertEqual(len(transport.requests), 1)

    def test_http_date_cooldown_and_request_context_are_shared_between_workers(self):
        timer = Clock()
        retry_date = format_datetime(datetime.fromtimestamp(1007, timezone.utc), usegmt=True)
        transport = FakeTransport(reply(503, **{"Retry-After": retry_date}), reply(body=b"other"), reply(body=b"same"))
        client = HttpClient(transport, sleep=timer.sleep, monotonic=timer.monotonic, clock=lambda: 1000)
        first = client.fork(item_id="a", attempt=1)
        with self.assertRaises(ArtworkError) as failure:
            first.get("https://a.mzstatic.com/a.png", purpose="image", max_attempts=1, deadline=300)
        self.assertTrue(failure.exception.retryable)
        client.fork(item_id="b", attempt=1).get("https://b.mzstatic.com/b.png", purpose="image", max_attempts=1, deadline=300)
        self.assertEqual(timer.delays, [])
        client.fork(item_id="a", attempt=2).get("https://a.mzstatic.com/a.png", purpose="image", max_attempts=1, deadline=300)
        self.assertEqual(timer.delays, [7])
        self.assertEqual(client.request_count, 3)

    def test_deadline_includes_redirect_time_cooldown_and_final_response(self):
        timer = Clock()

        class Transport(FakeTransport):
            def request(self, *args, **options):
                response = super().request(*args, **options)
                timer.now += 10
                return response

        transport = Transport(reply(302, Location="/next"), reply(503, **{"Retry-After": "35"}), reply(body=b"late"))
        client = HttpClient(transport, sleep=timer.sleep, monotonic=timer.monotonic)
        with self.assertRaises(ArtworkError) as failure:
            client.get("https://music.apple.com/", purpose="page")
        self.assertEqual(failure.exception.code, "NETWORK_TIMEOUT")
        self.assertEqual(timer.delays, [35])
        self.assertEqual([call[2] for call in transport.requests], [20, 20, 5])
        self.assertEqual(client.control.snapshot()["received_bytes"], 4)

    def test_three_transient_attempts_do_not_reset_at_redirects(self):
        timer = Clock()
        transport = FakeTransport(reply(503), reply(302, Location="/second"), reply(503),
                                  reply(302, Location="/third"), reply(503))
        with self.assertRaises(ArtworkError) as failure:
            HttpClient(transport, sleep=timer.sleep, monotonic=timer.monotonic).get("https://music.apple.com/", purpose="page")
        self.assertEqual(failure.exception.code, "HTTP_TRANSIENT_ERROR")
        self.assertEqual(len(transport.requests), 5)
        self.assertEqual(timer.delays, [1, 2])

    def test_cache_distinguishes_method_full_url_and_headers_and_excludes_images(self):
        calls = []

        class Transport:
            def request(self, url, **options):
                calls.append(("GET", url, options["headers"]))
                return HttpResponse(200, {}, b"metadata")

            def head(self, url, **options):
                calls.append(("HEAD", url, options["headers"]))
                return HttpResponse(200, {}, b"")

        client = HttpClient(Transport())
        url = "https://mvod.itunes.apple.com/synthetic/master.m3u8?revision=1"
        for _ in range(2):
            client.get(url, purpose="hls", headers={"Accept": "one"})
        client.get(url, purpose="hls", headers={"Accept": "two"})
        client.get(url.replace("=1", "=2"), purpose="hls", headers={"Accept": "one"})
        client.head(url, purpose="hls", headers={"Accept": "one"})
        for _ in range(2):
            client.get("https://a.mzstatic.com/a.png", purpose="image")
        self.assertEqual(len(calls), 6)
        self.assertEqual(client.control.snapshot()["cache_hits"], 1)

    def test_stream_partial_reset_is_one_attempt_then_new_sink_restarts_with_cooldown(self):
        timer = Clock()

        class ResetResponse(StreamResponse):
            def read(self, size):
                if self.body.tell() >= 4:
                    raise ConnectionResetError("synthetic private detail")
                return super().read(size)

        first, second = ResetResponse(), StreamResponse()
        client = HttpClient(MediaTransport(first, second), sleep=timer.sleep, monotonic=timer.monotonic)
        partial = BytesIO()
        with self.assertRaises(ArtworkError) as failure:
            client.fork(item_id="video", attempt=1).stream_to(MEDIA_URL, partial, expected_bytes=10,
                                                               max_attempts=1, deadline=300)
        self.assertTrue(failure.exception.retryable)
        self.assertEqual(partial.getvalue(), b"0123")
        self.assertEqual(client.request_count, 1)
        replacement = BytesIO()
        client.fork(item_id="video", attempt=2).stream_to(MEDIA_URL, replacement, expected_bytes=10,
                                                          max_attempts=1, deadline=300)
        self.assertEqual(replacement.getvalue(), b"0123456789")
        self.assertEqual(timer.delays, [1])
        self.assertEqual(client.request_count, 2)
        self.assertEqual(client.control.snapshot()["received_bytes"], 14)
        events = client.control.drain_events()
        self.assertEqual(events[-1]["attempt"], 2)
        self.assertTrue(first.closed and second.closed)

    def test_outer_second_attempt_uses_two_second_backoff_without_internal_retry(self):
        timer = Clock()
        transport = FakeTransport(TimeoutError(), reply(body=b"ok"))
        client = HttpClient(transport, sleep=timer.sleep, monotonic=timer.monotonic)
        with self.assertRaises(ArtworkError):
            client.fork(item_id="image", attempt=2).get("https://a.mzstatic.com/a.png", purpose="image", max_attempts=1)
        self.assertEqual(len(transport.requests), 1)
        client.fork(item_id="image", attempt=3).get("https://a.mzstatic.com/a.png", purpose="image", max_attempts=1)
        self.assertEqual(timer.delays, [2])

    def test_real_urllib_read1_checks_cancellation_and_counts_received_body(self):
        transport = UrllibTransport()
        transport._opener = Mock()
        client = HttpClient(transport)

        class Response(BytesIO):
            status = 200
            headers = {"Content-Type": "image/png"}
            def read(self, size):
                raise AssertionError("The fill-style read method must not be used")
            def read1(self, size):
                chunk = super().read1(min(size, 4))
                client.control.cancel()
                return chunk

        response = Response(b"partial-image")
        transport._opener.open.return_value = response
        with self.assertRaises(ArtworkError) as failure:
            client.get("https://a.mzstatic.com/a.png", purpose="image", max_attempts=1)
        self.assertEqual(failure.exception.code, "CANCELLED")
        self.assertFalse(failure.exception.retryable)
        self.assertEqual(client.control.snapshot()["received_bytes"], 4)
        self.assertTrue(response.closed)

    def test_production_forks_have_independent_openers_and_shared_control(self):
        parent = HttpClient()
        one, two = parent.fork(), parent.fork()
        self.assertIsNot(one.transport, two.transport)
        self.assertIsNot(one.transport._opener, two.transport._opener)
        self.assertIs(one.control, two.control)
