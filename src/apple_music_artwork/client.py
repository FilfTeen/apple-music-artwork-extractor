"""Bounded public Apple requests; credentials remain in memory only."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.client import HTTPException, HTTPResponse
from hashlib import sha256
import math
import re
import socket
from threading import Lock
import time
from typing import BinaryIO, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .errors import ArtworkError
from . import __version__
from .jsonio import loads_json
from .models import Target
from .runtime import RetryAfterDeadline, RuntimeControl


WEB_ORIGIN = "https://music.apple.com"
WEB_API_ORIGIN = "https://amp-api.music.apple.com"
CATALOG_API_ORIGIN = "https://api.music.apple.com"
MAX_API_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_MOTION_BYTES = 256 * 1024 * 1024
_HOSTS = {
    "page": {"music.apple.com"},
    "bundle": {"music.apple.com"},
    "web_api": {"amp-api.music.apple.com"},
    "catalog_api": {"api.music.apple.com"},
}
_RETRY_STATUSES = {429, 502, 503, 504}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    final_url: str | None = field(default=None, repr=False)


class Transport(Protocol):
    """One bounded GET without redirects.

    Media adapters additionally implement head(url, headers, timeout) and
    stream(url, headers, timeout), the latter returning a response context
    manager with status, headers and read(size). Missing capabilities fail
    explicitly; an injected adapter never falls back to a real connection.
    """

    def request(self, url: str, *, headers: Mapping[str, str], timeout: float,
                max_bytes: int) -> HttpResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """Use TLS verification, no cookies, no implicit proxy or redirect route."""

    def __init__(self):
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def fork(self):
        return type(self)()

    def _open(self, url, *, headers, timeout, method):
        request = Request(url, headers=dict(headers), method=method)
        try:
            return self._opener.open(request, timeout=timeout)
        except HTTPError as error:
            return error

    def request(self, url, *, headers, timeout, max_bytes):
        control = RuntimeControl()
        return self.request_controlled(url, headers=headers, timeout=timeout, max_bytes=max_bytes,
                                       control=control, deadline=control.deadline(60), category="api")

    def request_controlled(self, url, *, headers, timeout, max_bytes, control, deadline,
                           category, on_progress=None, before_body=None):
        with self._open(url, headers=headers, timeout=timeout, method="GET") as response:
            control.check(deadline, category)
            if response.status == 200 and before_body is not None:
                before_body(url, response.status, response.headers)
            status, response_headers = response.status, dict(response.headers)
            # Reflected error bodies are never read, retained or cached.
            if status != 200:
                return HttpResponse(status, response_headers, b"")
            raw_length = _header(response_headers, "Content-Length")
            expected = int(raw_length) if raw_length and re.fullmatch(r"[0-9]{1,18}", raw_length) else None
            if expected is not None and expected > max_bytes:
                raise ArtworkError("RESPONSE_TOO_LARGE", "Response exceeds the byte limit.", category)
            body = bytearray()
            if on_progress:
                on_progress(0, expected)
            while True:
                control.check(deadline, category)
                chunk = _read_piece(response, min(64 * 1024, max_bytes - len(body) + 1),
                                    min(timeout, deadline - control.monotonic()))
                if not isinstance(chunk, bytes):
                    raise ArtworkError("NETWORK_ERROR", "Transport returned invalid response bytes.", category)
                control.received(len(chunk))
                control.check(deadline, category)
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ArtworkError("RESPONSE_TOO_LARGE", "Response exceeds the byte limit.", category)
                if on_progress:
                    on_progress(len(body), expected)
            if expected is not None and len(body) != expected and _header(response_headers, "Transfer-Encoding") is None:
                raise ArtworkError("NETWORK_ERROR", "HTTP response ended before its declared length.", category)
            return HttpResponse(status, response_headers, bytes(body))

    def request_guarded(self, url, **options):
        """Explicit first-read guard capability, using the controlled read loop."""
        return self.request_controlled(url, **options)

    def head(self, url, *, headers, timeout):
        with self._open(url, headers=headers, timeout=timeout, method="HEAD") as response:
            return HttpResponse(response.status, dict(response.headers), b"")

    def stream(self, url, *, headers, timeout):
        return self._open(url, headers=headers, timeout=timeout, method="GET")


def _purpose_category(purpose):
    return "download" if purpose in {"image", "hls", "motion"} else "api"


def validate_url(url: str, purpose: str) -> str:
    """Validate the actual hostname, never a raw suffix or netloc prefix."""
    try:
        if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in url):
            raise ValueError
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.fragment or "\\" in url):
            raise ValueError
        if purpose == "image":
            allowed = host.endswith(".mzstatic.com") and host != ".mzstatic.com"
        elif purpose in {"hls", "motion"}:
            allowed = host == "mvod.itunes.apple.com" or host.endswith(".mvod.itunes.apple.com")
        else:
            allowed = host in _HOSTS.get(purpose, set())
        if not allowed or not re.fullmatch(r"[a-z0-9.-]+", host):
            raise ValueError
    except (ValueError, TypeError):
        category = _purpose_category(purpose)
        raise ArtworkError("UNSAFE_URL", "URL is outside the allowed HTTPS host boundary.", category) from None
    return url


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return next((str(value) for key, value in headers.items() if key.lower() == name.lower()), None)


def _metadata_body_guard(url, status, headers, *, purpose):
    """Reject declared media before reading, without guessing a text body's truth."""
    validate_url(url, purpose)
    types = [str(value) for key, value in headers.items() if key.lower() == "content-type"]
    mime = types[0].split(";", 1)[0].strip().lower() if len(types) == 1 else ""
    path = unquote(urlsplit(url).path).lower()
    media_path = re.search(r"\.(?:mp4|m4v|mov|m4s|ts|aac|mp3|jpg|jpeg|png|webp|avif|gif)(?:$|/)", path)
    allowed = {
        "page": {"text/html", "application/xhtml+xml"},
        "bundle": {"text/javascript", "application/javascript", "application/x-javascript"},
        "web_api": {"application/json"}, "catalog_api": {"application/json"},
        "hls": {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl"},
    }.get(purpose, set())
    if purpose == "hls" and path.endswith(".m3u8"):
        allowed = allowed | {"text/plain"}
    if media_path or mime not in allowed or any("," in value for value in types):
        raise ArtworkError("METADATA_BODY_BLOCKED", "The response is not unambiguously declared as allowed metadata.",
                           _purpose_category(purpose))


class _SerializedTransport:
    """Explicit serial adapter for injected transports without a clone hook."""

    def __init__(self, transport, lock, control):
        self._transport, self._lock, self._control = transport, lock, control

    @contextmanager
    def _locked(self, timeout):
        deadline = self._control.deadline(timeout)
        while not self._lock.acquire(timeout=0.05):
            self._control.check(deadline)
        try:
            self._control.check(deadline)
            yield
        finally:
            self._lock.release()

    def request(self, url, **options):
        with self._locked(options["timeout"]):
            return self._transport.request(url, **options)

    def request_guarded(self, url, **options):
        with self._locked(options["timeout"]):
            return self._transport.request_guarded(url, **options)

    def head(self, url, **options):
        operation = getattr(self._transport, "head", None)
        if not callable(operation):
            raise ArtworkError("TRANSPORT_CAPABILITY_MISSING", "The configured transport does not support HEAD requests.", "download")
        with self._locked(options["timeout"]):
            return operation(url, **options)

    @contextmanager
    def stream(self, url, **options):
        operation = getattr(self._transport, "stream", None)
        if not callable(operation):
            raise ArtworkError("TRANSPORT_CAPABILITY_MISSING", "The configured transport does not support streaming requests.", "download")
        with self._locked(options["timeout"]):
            with operation(url, **options) as response:
                yield response


def _origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or 443


def _read_piece(response, size, timeout):
    # CPython's HTTPResponse retains its socket through BufferedReader ->
    # SocketIO. Update the per-blocking-read timeout to the remaining deadline.
    # read1 avoids read(amt)'s fill loop; chunk headers and DNS still make this
    # cooperative cancellation, not a hard real-time networking guarantee.
    if isinstance(response, HTTPResponse):
        raw = getattr(getattr(response, "fp", None), "raw", None)
        connection = getattr(raw, "_sock", None)
        if connection is not None:
            connection.settimeout(timeout)
    reader = getattr(response, "read1", None)
    return reader(size) if callable(reader) else response.read(size)


class HttpClient:
    def __init__(self, transport: Transport | None = None, *,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, timeout: float = 20,
                 max_requests: int = 64, monotonic: Callable[[], float] = time.monotonic,
                 control: RuntimeControl | None = None, item_id=None, attempt=None):
        if not 0 < timeout <= 60 or not 1 <= max_requests <= 256:
            raise ValueError("Invalid transport limits")
        if attempt is not None and (type(attempt) is not int or not 1 <= attempt <= 3):
            raise ValueError("Invalid file attempt")
        self.transport = transport if transport is not None else UrllibTransport()
        self.control = control if control is not None else RuntimeControl(
            max_requests=max_requests, monotonic=monotonic, clock=clock,
            sleep=None if sleep is time.sleep else sleep)
        self.sleep, self.clock = sleep, self.control.clock
        self.timeout = min(timeout, 20)
        self.max_requests = self.control.max_requests
        self.monotonic = self.control.monotonic
        self.item_id, self.attempt = item_id, attempt
        self._adapter_lock = Lock()
        self._fresh_metadata = False

    @property
    def request_count(self):
        return self.control.snapshot()["requests"]

    def fork(self, *, item_id=None, attempt=None, transport=None):
        """Share command state; clone real transports or serialize injected ones."""
        if transport is None:
            clone = getattr(self.transport, "fork", None)
            if callable(clone):
                transport = clone()
                if transport is None or transport is self.transport:
                    raise ArtworkError("TRANSPORT_CAPABILITY_MISSING", "A transport clone must be an independent adapter.", "download")
            elif isinstance(self.transport, _SerializedTransport):
                transport = _SerializedTransport(self.transport._transport,
                                                 self.transport._lock, self.control)
            else:
                transport = _SerializedTransport(self.transport, self._adapter_lock, self.control)
        child = HttpClient(transport, control=self.control, timeout=self.timeout,
                           item_id=item_id, attempt=attempt)
        child._fresh_metadata = self._fresh_metadata
        return child

    def fresh_metadata_view(self):
        """Observe current metadata without reusing or mutating shared caches."""
        child = self.fork()
        child._fresh_metadata = True
        return child

    def _event(self, kind, **fields):
        event = {"kind": kind, "item_id": self.item_id, **fields}
        if self.attempt is not None:
            event.setdefault("attempt", self.attempt)
        self.control.emit(event)

    def _operation(self, name, category):
        underlying = self.transport._transport if isinstance(self.transport, _SerializedTransport) else self.transport
        if not callable(getattr(underlying, name, None)):
            raise ArtworkError("TRANSPORT_CAPABILITY_MISSING", "The configured transport lacks the requested HTTP operation.", category)
        return getattr(self.transport, name)

    def _deadline(self, deadline, seconds):
        return self.control.deadline(seconds) if deadline is None else deadline

    def _cache_key(self, url, purpose, headers, max_bytes, method):
        context = tuple(sorted((str(key).lower(), str(value)) for key, value in (headers or {}).items()))
        return method, purpose, url, max_bytes, sha256(repr(context).encode()).hexdigest()

    def get(self, url: str, *, purpose: str, headers: Mapping[str, str] | None = None,
            max_bytes: int = MAX_API_BYTES, max_attempts: int = 3, deadline=None,
            validate_response=None) -> HttpResponse:
        return self._request(url, purpose=purpose, headers=headers, max_bytes=max_bytes, method="GET",
                             max_attempts=max_attempts, deadline=deadline, validate_response=validate_response)

    def head(self, url: str, *, purpose: str, headers: Mapping[str, str] | None = None,
             max_attempts: int = 3, deadline=None, validate_response=None) -> HttpResponse:
        return self._request(url, purpose=purpose, headers=headers, max_bytes=1, method="HEAD",
                             max_attempts=max_attempts, deadline=deadline, validate_response=validate_response)

    def forget_response(self, url, *, purpose, headers=None, max_bytes=MAX_API_BYTES, method="GET"):
        if self._fresh_metadata:
            return
        key = self._cache_key(url, purpose, headers, 1 if method == "HEAD" else max_bytes, method)
        self.control.forget_response(key)

    def _request(self, url, *, purpose, headers, max_bytes, method, max_attempts, deadline, validate_response):
        if self._fresh_metadata and method == "GET" and purpose in {"image", "motion"}:
            raise ArtworkError("METADATA_BODY_BLOCKED", "Metadata observations cannot read media bodies.", _purpose_category(purpose))
        if not 0 < max_bytes <= 64 * 1024 * 1024:
            raise ValueError("Invalid response limit")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError("Invalid attempt limit")
        validate_url(url, purpose)
        deadline = self._deadline(deadline, 300 if method == "GET" and purpose == "image" else 60)
        category = _purpose_category(purpose)
        def loader():
            response = self._request_uncached(url, purpose=purpose, headers=headers,
                                              max_bytes=max_bytes, method=method,
                                              max_attempts=max_attempts, deadline=deadline)
            if response.status == 200 and validate_response is not None:
                validate_response(response)
            return response
        if not self._fresh_metadata and (purpose == "hls" or method == "HEAD"):
            key = self._cache_key(url, purpose, headers, max_bytes, method)
            response = self.control.request_cached(key, loader, deadline=deadline, category=category)
            # A cached response must satisfy this caller's constraints too.
            # No failing parser/HEAD validator publishes a cache entry.
            if response.status == 200 and validate_response is not None:
                try:
                    validate_response(response)
                except BaseException:
                    self.control.forget_response(key)
                    raise
        else:
            response = loader()
        # Cache entries are not exposed through mutable response header maps.
        return replace(response, headers=dict(response.headers))

    def _request_uncached(self, url, *, purpose, headers, max_bytes, method, max_attempts, deadline):
        current = url
        request_headers = {"User-Agent": f"AppleMusicArtwork/{__version__}", "Accept-Encoding": "identity", **dict(headers or {})}
        original_origin = _origin(current)
        category = _purpose_category(purpose)
        guarded = self._fresh_metadata and method == "GET"
        operation = self._operation("request_guarded" if guarded else "request" if method == "GET" else "head", category)
        failures = redirects = 0
        while True:
            self.control.acquire_request(_origin(current), deadline=deadline, category=category, item_id=self.item_id)
            try:
                remaining = deadline - self.monotonic()
                self.control.check(deadline, category)
                options = {"headers": request_headers, "timeout": min(self.timeout, remaining)}
                if method == "GET":
                    options["max_bytes"] = max_bytes
                controlled = getattr(self.transport, "request_controlled", None) if method == "GET" else None
                if guarded:
                    response = operation(current, **options, control=self.control, deadline=deadline,
                                         category=category, before_body=lambda url, status, headers:
                                         _metadata_body_guard(url, status, headers, purpose=purpose),
                                         on_progress=lambda received, expected: self._event(
                                             "transfer", received_bytes=received, expected_bytes=expected))
                elif callable(controlled):
                    response = controlled(current, **options, control=self.control, deadline=deadline,
                                          category=category, on_progress=lambda received, expected: self._event(
                                              "transfer", received_bytes=received, expected_bytes=expected))
                else:
                    response = operation(current, **options)
                    if isinstance(response.body, bytes):
                        self.control.received(len(response.body))
                self.control.check(deadline, category)
            except ArtworkError as error:
                if error.code == "RESPONSE_TOO_LARGE":
                    raise ArtworkError(error.code, "Response exceeds the byte limit.", category) from None
                raise
            except (HTTPException, OSError, URLError) as error:
                problem = self._network_error(error, category)
                if problem.retryable:
                    failures += 1
                    context = self._retry(current, {}, failures, max_attempts, deadline, category)
                    if failures < max_attempts:
                        continue
                    problem = ArtworkError(problem.code, problem.message, problem.category, True, context=context)
                raise problem from None
            if not isinstance(response.body, bytes) or len(response.body) > max_bytes:
                raise ArtworkError("RESPONSE_TOO_LARGE", "Response exceeds the byte limit.", category)
            if response.status in _RETRY_STATUSES:
                failures += 1
                context = self._retry(current, response.headers, failures, max_attempts, deadline, category)
                if failures < max_attempts:
                    continue
                context["http_status"] = response.status
                if response.status == 429:
                    context["reason"] = "server_cooldown"
                raise ArtworkError("HTTP_TRANSIENT_ERROR", f"HTTPS request returned HTTP {response.status}.", category, True,
                                   context=context)
            if response.status not in _REDIRECT_STATUSES:
                return replace(response, final_url=current, headers=dict(response.headers))
            if redirects >= 5:
                raise ArtworkError("REDIRECT_LIMIT", "Too many redirects.", category)
            current = self._redirect(current, response.headers, request_headers, original_origin, purpose)
            redirects += 1

    def _network_error(self, error, category):
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, (TimeoutError, socket.timeout, ConnectionResetError)):
            return ArtworkError("NETWORK_ERROR", "HTTPS request timed out or connection reset.", category, True)
        return ArtworkError("NETWORK_ERROR", "HTTP response failed, was incomplete or malformed.", category)

    def _retry(self, url, headers, failures, max_attempts, deadline, category):
        self.control.check(deadline, category)
        index = (self.attempt or 1) + failures - 2
        delay = self._retry_delay(headers, index)
        # Keep the parsed server/backoff declaration as the lower bound; clock
        # sampling must not turn Retry-After: 600 into a shorter public wait.
        delay = max(delay, self.control.cooldown(_origin(url), delay))
        if delay >= deadline - self.monotonic():
            raise RetryAfterDeadline(delay, self.clock() + delay, category)
        if failures < max_attempts:
            self._event("retry", retry_after_seconds=delay, attempt=failures + 1)
        return {"retry_at": self.clock() + delay,
                "reason": "server_cooldown" if _header(headers, "Retry-After") else "transient_network"}

    def _redirect(self, current, response_headers, request_headers, original_origin, purpose):
        location = _header(response_headers, "Location")
        if not location:
            raise ArtworkError("INVALID_REDIRECT", "Redirect lacks a destination.", _purpose_category(purpose))
        destination = validate_url(urljoin(current, location), purpose)
        if _header(request_headers, "Authorization") and _origin(destination) != original_origin:
            raise ArtworkError("AUTH_REDIRECT_BLOCKED", "Authenticated cross-origin redirect blocked.", "auth")
        return destination

    def stream_to(self, url: str, sink: BinaryIO, *, purpose: str = "motion",
                  expected_bytes: int, max_bytes: int = MAX_MOTION_BYTES,
                  max_duration: float = 300, headers: Mapping[str, str] | None = None,
                  max_attempts: int = 3, deadline=None) -> dict:
        """One complete body; retries after partial writes belong to new-temp callers."""
        if self._fresh_metadata:
            raise ArtworkError("METADATA_BODY_BLOCKED", "Metadata observations cannot stream media bodies.", _purpose_category(purpose))
        category = _purpose_category(purpose)
        if type(expected_bytes) is not int or expected_bytes <= 0 or type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("Invalid streaming byte limits")
        if expected_bytes > max_bytes:
            raise ArtworkError("RESPONSE_TOO_LARGE", "Declared media size exceeds the video byte limit.", category)
        if not 0 < max_duration <= 300 or type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError("Invalid streaming limits")
        operation = self._operation("stream", category)
        current = validate_url(url, purpose)
        request_headers = {"User-Agent": f"AppleMusicArtwork/{__version__}", "Accept-Encoding": "identity", **dict(headers or {})}
        original_origin = _origin(current)
        deadline = self._deadline(deadline, max_duration)
        written = failures = redirects = 0
        digest = sha256()
        self._event("transfer", received_bytes=0, expected_bytes=expected_bytes)
        while True:
            self.control.acquire_request(_origin(current), deadline=deadline, category=category, item_id=self.item_id)
            try:
                self.control.check(deadline, category)
                with operation(current, headers=request_headers, timeout=min(self.timeout, deadline - self.monotonic())) as response:
                    self.control.check(deadline, category)
                    if response.status in _RETRY_STATUSES:
                        failures += 1
                        context = self._retry(current, response.headers, failures, max_attempts, deadline, category)
                        if failures < max_attempts:
                            continue
                        context["http_status"] = response.status
                        if response.status == 429:
                            context["reason"] = "server_cooldown"
                        raise ArtworkError("HTTP_TRANSIENT_ERROR", f"Media request returned HTTP {response.status}.", category, True,
                                           context=context)
                    if response.status in _REDIRECT_STATUSES:
                        if redirects >= 5:
                            raise ArtworkError("REDIRECT_LIMIT", "Too many redirects.", category)
                        current = self._redirect(current, response.headers, request_headers, original_origin, purpose)
                        redirects += 1
                        continue
                    if response.status != 200:
                        raise ArtworkError("DOWNLOAD_FAILED", f"Media request returned HTTP {response.status}.", category)
                    raw_length = _header(response.headers, "Content-Length")
                    if raw_length is not None and (re.fullmatch(r"[0-9]{1,18}", raw_length) is None
                                                   or int(raw_length) != expected_bytes):
                        raise ArtworkError("DOWNLOAD_LENGTH_MISMATCH", "Media length changed after inspection.", category)
                    encoding = _header(response.headers, "Content-Encoding")
                    if encoding and encoding.lower() != "identity":
                        raise ArtworkError("MEDIA_TYPE_MISMATCH", "Encoded media transfer is unsupported.", category)
                    content_type = (_header(response.headers, "Content-Type") or "").split(";", 1)[0].strip().lower()
                    if content_type not in {"video/mp4", "application/mp4", "application/octet-stream"}:
                        raise ArtworkError("MEDIA_TYPE_MISMATCH", "Media response is not declared as MP4.", category)
                    while True:
                        self.control.check(deadline, category)
                        chunk = _read_piece(response, min(256 * 1024, expected_bytes - written + 1),
                                            min(self.timeout, deadline - self.monotonic()))
                        if not isinstance(chunk, bytes):
                            raise ArtworkError("NETWORK_ERROR", "Media transport returned invalid bytes.", category)
                        self.control.received(len(chunk))
                        self.control.check(deadline, category)
                        if not chunk:
                            break
                        if written + len(chunk) > expected_bytes or written + len(chunk) > max_bytes:
                            raise ArtworkError("DOWNLOAD_LENGTH_MISMATCH", "Media exceeds its inspected byte length.", category)
                        try:
                            count = sink.write(chunk)
                        except OSError:
                            raise ArtworkError("OUTPUT_WRITE_FAILED", "Unable to write the owned media temporary file.", "download") from None
                        if count != len(chunk):
                            raise ArtworkError("OUTPUT_WRITE_FAILED", "Media output write was incomplete.", "download")
                        digest.update(chunk)
                        written += len(chunk)
                        self._event("transfer", received_bytes=written, expected_bytes=expected_bytes)
                    if written != expected_bytes:
                        raise ArtworkError("DOWNLOAD_LENGTH_MISMATCH", "Media ended before its inspected byte length.", category)
                    return {"url": current, "bytes": written, "sha256": digest.hexdigest(), "content_type": content_type}
            except ArtworkError:
                raise
            except (HTTPException, OSError, URLError) as error:
                problem = self._network_error(error, category)
                if problem.retryable:
                    failures += 1
                    context = self._retry(current, {}, failures, max_attempts if written == 0 else 1, deadline, category)
                    if written == 0 and failures < max_attempts:
                        continue
                    problem = ArtworkError(problem.code, problem.message, problem.category, True, context=context)
                raise problem from None

    def _retry_delay(self, headers, attempt):
        raw = _header(headers, "Retry-After")
        if raw:
            raw = raw.strip()
            if re.fullmatch(r"[0-9]+", raw):
                # Larger integer values all exceed our bounded operation
                # deadlines; avoid unbounded integer parsing of remote text.
                return int(raw) if len(raw) <= 18 else 10 ** 18
            try:
                parsed = parsedate_to_datetime(raw)
                if parsed.tzinfo is not None:
                    seconds = parsed.timestamp() - self.clock()
                    if math.isfinite(seconds):
                        return max(seconds, 0)
            except (TypeError, ValueError, OverflowError):
                pass
        return 2 ** min(max(attempt, 0), 2)

class _ScriptSources(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "script":
            source = dict(attrs).get("src")
            if source and source not in self.sources:
                self.sources.append(source)


@dataclass(frozen=True)
class _Token:
    value: str = field(repr=False)


class AppleMusicClient:
    """Official catalog or experimental public WebPlay discovery adapter."""

    def __init__(self, http: HttpClient | None = None, *, provider: str = "web",
                 developer_token: str | None = None):
        if provider not in {"web", "catalog"}:
            raise ArtworkError("INVALID_PROVIDER", "Choose web or catalog.", "input")
        self.http = http or HttpClient()
        self.provider = provider
        self._token = _Token(developer_token) if developer_token else None

    def fetch(self, target: Target) -> object:
        endpoint = self._endpoint(target)
        if self.provider == "catalog":
            if self._token is None:
                raise ArtworkError("TOKEN_REQUIRED", "Catalog access requires the specified developer-token environment variable.", "auth")
            return self._api(endpoint, self._token)
        if self._token is not None:
            try:
                return self._api(endpoint, self._token)
            except ArtworkError as error:
                if error.code != "TOKEN_REJECTED":
                    raise
                self._token = None
                rounds = 1
        else:
            rounds = 2
        for _ in range(rounds):
            for candidate in self._discover_tokens():
                try:
                    payload = self._api(endpoint, candidate)
                except ArtworkError as error:
                    if error.code == "TOKEN_REJECTED":
                        continue
                    raise
                self._token = candidate
                return payload
        raise ArtworkError("TOKEN_REJECTED", "Public WebPlay credentials were rejected after one refresh.", "auth")

    def _endpoint(self, target):
        key = target.key
        origin = WEB_API_ORIGIN if self.provider == "web" else CATALOG_API_ORIGIN
        parts = (key.storefront, key.resource_type, key.resource_id)
        endpoint = origin + "/v1/catalog/" + "/".join(quote(part, safe="") for part in parts)
        params = {}
        if self.provider == "web":
            params = {"platform": "web", "art[url]": "c,f",
                      f"extend[{key.resource_type}]": "editorialArtwork,editorialVideo"}
        if target.locale:
            params["l"] = target.locale
        return endpoint + ("?" + urlencode(params) if params else "")

    def _api(self, endpoint, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,16384}", token.value):
            raise ArtworkError("TOKEN_INVALID", "Developer token has an invalid representation.", "auth")
        headers = {"Authorization": "Bearer " + token.value, "Accept": "application/json"}
        if self.provider == "web":
            headers.update({"Origin": WEB_ORIGIN, "Referer": WEB_ORIGIN + "/"})
        response = self.http.get(endpoint, purpose=f"{self.provider}_api", headers=headers)
        if response.status == 401:
            raise ArtworkError("TOKEN_REJECTED", "Apple rejected the credential.", "auth")
        if response.status == 404:
            raise ArtworkError("RESOURCE_NOT_FOUND", "Resource was not found in this storefront.", "api")
        if response.status != 200:
            raise ArtworkError("API_HTTP_ERROR", f"Apple returned HTTP {response.status}.", "api",
                               response.status in _RETRY_STATUSES)
        try:
            payload = loads_json(response.body)
        except ArtworkError:
            raise ArtworkError("API_RESPONSE_INVALID", "Apple returned invalid JSON.", "api") from None
        if not isinstance(payload, dict) or not ({"data", "resources"} & payload.keys()):
            raise ArtworkError("API_SCHEMA_CHANGED", "Apple response lacks resource data.", "api")
        return payload

    def _discover_tokens(self):
        response = self.http.get(WEB_ORIGIN + "/", purpose="page", max_bytes=4 * 1024 * 1024)
        if response.status != 200:
            raise ArtworkError("TOKEN_DISCOVERY_FAILED", "Unable to load public Apple Music bootstrap.", "auth")
        parser = _ScriptSources()
        parser.feed(response.body.decode("utf-8", errors="replace"))
        candidates = []
        seen = set()
        # Only same-origin public bundles; never follow arbitrary script hosts.
        scripts = []
        for source in parser.sources:
            try:
                scripts.append(validate_url(urljoin(WEB_ORIGIN + "/", source), "bundle"))
            except ArtworkError:
                continue
            if len(scripts) == 8:
                break
        for script in scripts:
            bundle = self.http.get(script, purpose="bundle", max_bytes=16 * 1024 * 1024)
            if bundle.status != 200:
                continue
            text = bundle.body.decode("utf-8", errors="replace")
            for value in re.findall(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", text):
                if value in seen or len(value) > 16384:
                    continue
                seen.add(value)
                try:
                    header, payload, _ = value.split(".")
                    header = loads_json(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
                    payload = loads_json(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
                    expiry = payload.get("exp") if isinstance(payload, dict) else None
                    if (not isinstance(header, dict) or header.get("kid") != "WebPlayKid"
                            or header.get("alg") != "ES256" or type(expiry) not in (int, float)
                            or not math.isfinite(expiry) or expiry <= self.http.clock()):
                        continue
                except (ArtworkError, ValueError, TypeError, OverflowError):
                    continue
                # Header/expiry screening is not cryptographic verification.
                candidates.append(_Token(value))
                if len(candidates) == 8:
                    return candidates
        if not candidates:
            raise ArtworkError("TOKEN_DISCOVERY_FAILED", "No current public WebPlay candidates found; Apple may have changed its bootstrap.", "auth")
        return candidates
