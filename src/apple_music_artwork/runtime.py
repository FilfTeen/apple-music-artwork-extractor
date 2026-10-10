"""Command-local cancellation, HTTP accounting and worker progress events."""

from __future__ import annotations

from collections import deque
import math
from threading import Condition, Event, Lock
import time

from .errors import ArtworkError


class RetryAfterDeadline(ArtworkError):
    """A server cooldown cannot fit inside the remaining logical deadline."""

    def __init__(self, seconds, retry_at, category):
        super().__init__("RETRY_AFTER_DEADLINE", "The retry wait exceeds this operation's deadline; retry the same task later.", category,
                         context={"reason": "server_cooldown", "retry_at": retry_at})
        self.retry_after_seconds = seconds
        self.retry_at = retry_at

    def to_dict(self):
        return {**super().to_dict(), "retry_after_seconds": self.retry_after_seconds,
                "retry_at": self.retry_at}


class RuntimeControl:
    """One command's shared state; no files, credentials or terminal output."""

    def __init__(self, *, max_requests=64, monotonic=time.monotonic,
                 clock=time.time, sleep=None):
        if type(max_requests) is not int or not 1 <= max_requests <= 256:
            raise ValueError("Invalid request budget")
        self.max_requests = max_requests
        self.monotonic, self.clock = monotonic, clock
        self._sleep = sleep
        self._cancel_requested = False
        self._cancel = Event()
        self._lock = Lock()
        self._condition = Condition(self._lock)
        self._requests = self._received_bytes = self._cache_hits = 0
        self._window_requests, self._window_id = 0, 1
        self._cooldowns = {}
        self._events = deque()
        self._cache = {}
        self._inflight = set()

    @property
    def cancelled(self):
        return self._cancel_requested

    def request_cancel(self):
        """Signal-safe request: no Event, Lock or Condition operations here."""
        self._cancel_requested = True

    def cancel(self):
        """Request and wake waiters from normal, non-signal context."""
        self.request_cancel()
        self._cancel.set()
        with self._condition:
            self._condition.notify_all()

    def deadline(self, seconds):
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Invalid operation deadline")
        return self.monotonic() + seconds

    def check(self, deadline=None, category="download"):
        if self.cancelled:
            raise ArtworkError("CANCELLED", "The operation was cancelled.", category)
        if deadline is not None and self.monotonic() >= deadline:
            raise ArtworkError("NETWORK_TIMEOUT", "Operation exceeded its total time limit.", category)

    def emit(self, event):
        # Keep high-rate progress bounded even when the coordinator is in a
        # local commit. Terminal renderers receive only the newest progress.
        event = dict(event)
        with self._lock:
            if event.get("kind") in {"transfer", "decode"}:
                key = (event.get("kind"), event.get("item_id"))
                for index in range(len(self._events) - 1, -1, -1):
                    previous = self._events[index]
                    if previous.get("kind") not in {"transfer", "decode"}:
                        break
                    if (previous.get("kind"), previous.get("item_id")) == key:
                        self._events[index] = event
                        return
            self._events.append(event)

    def drain_events(self):
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def snapshot(self):
        with self._lock:
            return {"requests": self._requests, "received_bytes": self._received_bytes,
                    "cache_hits": self._cache_hits}

    def budget_snapshot(self):
        with self._lock:
            return {"window_id": self._window_id, "window_requests": self._window_requests,
                    "max_requests": self.max_requests}

    def begin_attempt_window(self):
        """Coordinator only, after workers stop; retain history and cooldowns."""
        self.check()
        with self._lock:
            self.check()
            if self._inflight:
                raise ArtworkError("RUNTIME_BUSY", "Cannot restart a request window while metadata work is active.", "download")
            self._window_requests = 0
            self._window_id += 1
        return self.budget_snapshot()

    def received(self, count):
        if type(count) is not int or count < 0:
            raise ValueError("Invalid received byte count")
        with self._lock:
            self._received_bytes += count

    def wait(self, seconds, *, deadline, category="download"):
        self.check(deadline, category)
        if seconds <= 0:
            return
        remaining = deadline - self.monotonic()
        if seconds >= remaining:
            raise RetryAfterDeadline(seconds, self.clock() + seconds, category)
        if self._sleep is None:
            self._cancel.wait(seconds)
        else:
            # An injected sleeper is the deterministic-clock seam; production
            # uses Event.wait so cancellation wakes it immediately.
            self._sleep(seconds)
        self.check(deadline, category)

    def cooldown(self, origin, seconds):
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Invalid server cooldown")
        until = self.monotonic() + seconds
        with self._lock:
            previous = self._cooldowns.get(origin)
            if previous is None or until > previous[0]:
                self._cooldowns[origin] = (until, 1 if previous is None else previous[1] + 1)
            return max(0, self._cooldowns[origin][0] - self.monotonic())

    def acquire_request(self, origin, *, deadline, category, item_id=None):
        """Wait for this origin, then atomically reserve one real request."""
        waited = None
        while True:
            self.check(deadline, category)
            with self._lock:
                if self._window_requests >= self.max_requests:
                    raise ArtworkError("REQUEST_LIMIT", "Request budget exhausted.", category)
                cooldown = self._cooldowns.get(origin)
                delay = max(0, cooldown[0] - self.monotonic()) if cooldown else 0
                if delay == 0 or cooldown == waited:
                    self._requests += 1
                    self._window_requests += 1
                    return
            self.emit({"kind": "cooldown", "item_id": item_id, "retry_after_seconds": delay})
            self.wait(delay, deadline=deadline, category=category)
            # If a different worker extended this origin's wait, observe that
            # newer cooldown before reserving the request. Injected clocks may
            # simulate the completed wait without advancing wall time.
            waited = cooldown

    def request_cached(self, key, loader, *, deadline, category):
        """Coalesce matching successful metadata requests within this command."""
        with self._condition:
            while key in self._inflight:
                self.check(deadline, category)
                self._condition.wait(min(0.1, max(0, deadline - self.monotonic())))
            self.check(deadline, category)
            if key in self._cache:
                self._cache_hits += 1
                return self._cache[key]
            self._inflight.add(key)
        try:
            response = loader()
            if response.status == 200:
                with self._lock:
                    self._cache[key] = response
            return response
        finally:
            with self._condition:
                self._inflight.remove(key)
                self._condition.notify_all()

    def forget_response(self, key):
        with self._lock:
            self._cache.pop(key, None)
