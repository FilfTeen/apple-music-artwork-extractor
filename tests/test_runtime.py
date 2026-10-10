"""Command-local concurrency checks use barriers and events, never live HTTP."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
from textwrap import dedent
from threading import Barrier, Event, Lock
from unittest import TestCase, skipUnless

from apple_music_artwork.client import HttpClient, HttpResponse
from apple_music_artwork.errors import ArtworkError
from apple_music_artwork.runtime import RuntimeControl


class RuntimeTests(TestCase):
    def test_new_attempt_window_retains_cumulative_counts_and_origin_cooldown(self):
        control = RuntimeControl(max_requests=1, monotonic=lambda: 0, clock=lambda: 1000)
        self.assertTrue(callable(getattr(control, "begin_attempt_window", None)))
        control.acquire_request("origin", deadline=60, category="download")
        control.received(5)
        with self.assertRaises(ArtworkError) as exhausted:
            control.acquire_request("origin", deadline=60, category="download")
        self.assertEqual(exhausted.exception.code, "REQUEST_LIMIT")
        control.cooldown("origin", 600)
        control.begin_attempt_window()
        self.assertEqual(control.snapshot(), {"requests": 1, "received_bytes": 5, "cache_hits": 0})
        self.assertEqual(control.budget_snapshot(), {"window_id": 2, "window_requests": 0, "max_requests": 1})
        with self.assertRaises(ArtworkError) as cooling:
            control.acquire_request("origin", deadline=60, category="download")
        self.assertEqual(cooling.exception.code, "RETRY_AFTER_DEADLINE")
        self.assertEqual(control.snapshot()["requests"], 1)
        control.acquire_request("other", deadline=60, category="download")
        self.assertEqual(control.snapshot()["requests"], 2)

    def test_new_window_cannot_clear_cancellation_or_restart_inflight_metadata(self):
        control = RuntimeControl()
        self.assertTrue(callable(getattr(control, "begin_attempt_window", None)))
        control._inflight.add("synthetic")
        with self.assertRaises(ArtworkError) as active:
            control.begin_attempt_window()
        self.assertEqual(active.exception.code, "RUNTIME_BUSY")
        control._inflight.clear()
        control.cancel()
        with self.assertRaises(ArtworkError) as cancelled:
            control.begin_attempt_window()
        self.assertEqual(cancelled.exception.code, "CANCELLED")
        self.assertEqual(control.budget_snapshot()["window_id"], 1)

    @skipUnless(os.name == "posix", "Real SIGINT delivery is exercised on POSIX")
    def test_repeated_sigint_returns_while_runtime_or_event_lock_is_held(self):
        # A deadlock must fail this isolated child, not freeze the test runner.
        # subprocess.run kills and reaps its own child if the timeout expires.
        script = dedent("""\
            import json, os, signal, sys
            from apple_music_artwork.errors import ArtworkError
            from apple_music_artwork.execution import _Cancellation
            from apple_music_artwork.runtime import RuntimeControl

            control = RuntimeControl()
            previous = signal.getsignal(signal.SIGINT)
            lock = control._lock if sys.argv[1] == "runtime" else control._cancel._cond
            handled = 0
            with _Cancellation(control) as cancellation:
                with lock:
                    for _ in range(3):
                        os.kill(os.getpid(), signal.SIGINT)
                        assert control.cancelled and cancellation.user_cancelled
                        try:
                            control.check()
                        except ArtworkError as error:
                            assert error.code == "CANCELLED"
                        else:
                            raise AssertionError("Signal cancellation must be immediately visible")
                        handled += 1
                    assert not control._cancel.is_set(), "Handler must only request cancellation"
                control.cancel()
                assert control._cancel.is_set(), "Normal context must wake waiters"
            assert signal.getsignal(signal.SIGINT) is previous
            print(json.dumps({"handled": handled, "restored": True}))
            """)
        for held in ("runtime", "event"):
            with self.subTest(held_lock=held):
                result = subprocess.run([sys.executable, "-B", "-c", script, held],
                                        capture_output=True, text=True, timeout=3, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {"handled": 3, "restored": True})

    def test_four_workers_share_one_exact_request_budget_and_byte_counter(self):
        ready = Barrier(4)
        adapters = []
        adapter_lock = Lock()

        class Transport:
            def fork(self):
                child = Transport()
                with adapter_lock:
                    adapters.append(child)
                return child

            def request(self, url, **options):
                return HttpResponse(200, {}, b"body")

        parent = HttpClient(Transport())
        children = [parent.fork(item_id=str(index), attempt=1) for index in range(4)]

        def work(client):
            ready.wait(timeout=2)
            results = []
            for index in range(20):
                try:
                    client.get(f"https://a.mzstatic.com/{index}.png", purpose="image", max_attempts=1)
                    results.append("ok")
                except ArtworkError as error:
                    results.append(error.code)
            return results

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = [item for batch in pool.map(work, children) for item in batch]
        self.assertEqual(results.count("ok"), 64)
        self.assertEqual(results.count("REQUEST_LIMIT"), 16)
        self.assertEqual(parent.control.snapshot(), {"requests": 64, "received_bytes": 256, "cache_hits": 0})
        self.assertEqual(len({id(adapter) for adapter in adapters}), 4)
        self.assertTrue(all(child.control is parent.control for child in children))

    def test_identical_parallel_metadata_requests_share_one_success(self):
        entered, release = Event(), Event()
        calls = []

        class Transport:
            def request(self, url, **options):
                calls.append(url)
                entered.set()
                if not release.wait(2):
                    raise AssertionError("Test did not release metadata request")
                return HttpResponse(200, {"Content-Type": "application/vnd.apple.mpegurl"}, b"#EXTM3U\n")

        parent = HttpClient(Transport())
        url = "https://mvod.itunes.apple.com/synthetic/master.m3u8"
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(parent.fork(item_id="a").get, url, purpose="hls")
            self.assertTrue(entered.wait(2))
            second = pool.submit(parent.fork(item_id="b").get, url, purpose="hls")
            release.set()
            a, b = first.result(timeout=2), second.result(timeout=2)
        self.assertEqual(a.body, b.body)
        a.headers["Content-Type"] = "changed"
        self.assertEqual(b.headers["Content-Type"], "application/vnd.apple.mpegurl")
        self.assertEqual(len(calls), 1)
        self.assertEqual(parent.control.snapshot()["cache_hits"], 1)

    def test_cancellation_wakes_origin_cooldown_without_consuming_budget(self):
        entered = Event()

        class Control(RuntimeControl):
            def emit(self, event):
                super().emit(event)
                if event["kind"] == "cooldown":
                    entered.set()

        control = Control()
        origin = ("https", "mvod.itunes.apple.com", 443)
        control.cooldown(origin, 30)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(control.acquire_request, origin, deadline=control.deadline(60), category="download")
            self.assertTrue(entered.wait(2))
            control.request_cancel()
            self.assertTrue(control.cancelled)
            self.assertFalse(control._cancel.is_set())
            control.cancel()
            with self.assertRaises(ArtworkError) as failure:
                future.result(timeout=2)
        self.assertEqual(failure.exception.code, "CANCELLED")
        self.assertEqual(control.snapshot()["requests"], 0)

    def test_signal_request_then_normal_cancel_wakes_a_blocked_cache_waiter(self):
        loaded, waiting, release = Event(), Event(), Event()
        control = RuntimeControl()
        original_wait = control._condition.wait

        def observed_wait(timeout=None):
            waiting.set()
            # Stretch only this test's wait to prove notification, rather than
            # succeeding because the production 0.1-second poll elapsed.
            return original_wait(5)

        control._condition.wait = observed_wait

        def loader():
            loaded.set()
            if not release.wait(5):
                raise AssertionError("Test did not release cache loader")
            return HttpResponse(200, {}, b"body")

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(control.request_cached, "key", loader,
                                deadline=control.deadline(60), category="download")
            self.assertTrue(loaded.wait(2))
            second = pool.submit(control.request_cached, "key", loader,
                                 deadline=control.deadline(60), category="download")
            try:
                self.assertTrue(waiting.wait(2))
                control.request_cancel()
                self.assertTrue(control.cancelled)
                self.assertFalse(control._cancel.is_set())
                control.cancel()
                with self.assertRaises(ArtworkError) as failure:
                    second.result(timeout=2)
                self.assertEqual(failure.exception.code, "CANCELLED")
                self.assertFalse(first.done(), "Cancelling a cache waiter must not require the loader to finish")
            finally:
                release.set()
            first.result(timeout=2)

    def test_cooldown_is_origin_scoped_and_extension_is_observed(self):
        now = [0.0]
        waits = []
        first = ("https", "a.mzstatic.com", 443)
        other = ("https", "b.mzstatic.com", 443)
        control = None

        def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds
            if len(waits) == 1:
                control.cooldown(first, 2)

        control = RuntimeControl(monotonic=lambda: now[0], sleep=sleep)
        control.cooldown(first, 7)
        control.acquire_request(other, deadline=20, category="download")
        self.assertEqual(waits, [])
        control.acquire_request(first, deadline=20, category="download")
        self.assertEqual(waits, [7, 2])
        self.assertEqual(control.snapshot()["requests"], 2)

    def test_cancelled_cache_waiter_cannot_reuse_a_late_result(self):
        entered, release = Event(), Event()
        control = RuntimeControl()

        def loader():
            entered.set()
            self.assertTrue(release.wait(2))
            return HttpResponse(200, {}, b"body")

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(control.request_cached, "key", loader, deadline=control.deadline(60), category="download")
            self.assertTrue(entered.wait(2))
            second = pool.submit(control.request_cached, "key", loader, deadline=control.deadline(60), category="download")
            control.cancel()
            release.set()
            first.result(timeout=2)
            with self.assertRaises(ArtworkError) as failure:
                second.result(timeout=2)
        self.assertEqual(failure.exception.code, "CANCELLED")

    def test_progress_coalesces_without_losing_stage_boundaries(self):
        control = RuntimeControl()
        for received in range(10000):
            control.emit({"kind": "transfer", "item_id": "file", "received_bytes": received})
        control.emit({"kind": "stage", "item_id": "file", "stage": "validate"})
        control.emit({"kind": "decode", "item_id": "file", "processed_seconds": 1})
        events = control.drain_events()
        self.assertEqual([event["kind"] for event in events], ["transfer", "stage", "decode"])
        self.assertEqual(events[0]["received_bytes"], 9999)
        self.assertEqual(control.drain_events(), [])
