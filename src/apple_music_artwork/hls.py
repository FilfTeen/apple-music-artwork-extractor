"""Inspect a bounded HLS subset without fetching any media or encryption keys."""

from __future__ import annotations

from dataclasses import dataclass, replace
import importlib
import math
import re
from urllib.parse import urljoin, urlsplit

from .client import HttpClient, MAX_MOTION_BYTES, _header, validate_url
from .errors import ArtworkError
from .models import VisualAsset


MAX_PLAYLIST_BYTES = 1024 * 1024
MAX_VARIANTS = 128
MAX_SEGMENTS = 1024
_VIDEO_CODECS = {"avc1": "h264", "avc3": "h264", "hvc1": "hevc", "hev1": "hevc"}
_ATTRIBUTE_TAGS = {"#EXT-X-KEY", "#EXT-X-SESSION-KEY", "#EXT-X-MAP", "#EXT-X-STREAM-INF",
                   "#EXT-X-I-FRAME-STREAM-INF", "#EXT-X-MEDIA", "#EXT-X-SESSION-DATA",
                   "#EXT-X-START", "#EXT-X-DEFINE", "#EXT-X-DATERANGE"}


def require_parser():
    """Only motion operations require this optional, text-only parser."""
    try:
        parser = importlib.import_module("m3u8")
    except ImportError:
        raise ArtworkError("MOTION_DEPENDENCY_MISSING", "Motion inspection requires the optional m3u8 package; install the motion extra.", "download") from None
    if not callable(getattr(parser, "loads", None)):
        raise ArtworkError("MOTION_DEPENDENCY_MISSING", "The installed m3u8 package has no compatible text parser.", "download")
    return parser


@dataclass(frozen=True)
class MotionVariant:
    url: str
    width: int | None
    height: int | None
    codecs: str
    frame_rate: float | None
    average_bandwidth: int | None
    bandwidth: int | None
    video_range: str | None = None
    stable_variant_id: str | None = None
    audio_group: str | None = None
    video_group: str | None = None

    @property
    def codec(self):
        values = [value.strip().split(".", 1)[0] for value in self.codecs.split(",")]
        return _VIDEO_CODECS.get(values[0]) if len(values) == 1 else None

    def to_dict(self):
        return {"url": self.url, "width": self.width, "height": self.height,
                "codecs": self.codecs, "codec": self.codec, "frame_rate": self.frame_rate,
                "average_bandwidth": self.average_bandwidth, "bandwidth": self.bandwidth,
                "video_range": self.video_range, "stable_variant_id": self.stable_variant_id,
                "audio_group": self.audio_group, "video_group": self.video_group}


@dataclass(frozen=True)
class MotionLayout:
    mp4_url: str
    source_mp4_url: str
    media_url: str
    content_length: int
    duration: float
    segment_count: int
    ranges: tuple[tuple[int, int], ...]

    def to_dict(self):
        return {"kind": "complete_single_mp4", "mp4_url": self.mp4_url,
                "source_mp4_url": self.source_mp4_url, "media_url": self.media_url,
                "content_length": self.content_length, "duration": self.duration,
                "segment_count": self.segment_count,
                "ranges": [{"offset": offset, "length": length} for offset, length in self.ranges],
                "ended": True, "encrypted": False, "whole_file_coverage": True}

    def provisional_dict(self):
        return {"source_mp4_url": self.source_mp4_url, "media_url": self.media_url,
                "content_length": self.content_length, "duration": self.duration,
                "segment_count": self.segment_count,
                "ranges": [{"offset": offset, "length": length} for offset, length in self.ranges],
                "head_verified": False}


@dataclass(frozen=True)
class MotionInspection:
    asset_id: str
    master_url: str
    profile: str = "best"
    status: str = "unsupported"
    variants: tuple[MotionVariant, ...] = ()
    selected: MotionVariant | None = None
    layout: MotionLayout | None = None
    error: ArtworkError | None = None
    resolved_master_url: str | None = None
    iframe_variant_count: int = 0
    provisional_layout: MotionLayout | None = None

    @property
    def supported(self):
        return self.status == "supported" and self.layout is not None and self.error is None

    @property
    def request_key(self):
        return (self.layout.mp4_url, self.layout.content_length) if self.supported else None

    def to_dict(self):
        return {"asset_id": self.asset_id, "master_url": self.master_url,
                "resolved_master_url": self.resolved_master_url, "profile": self.profile,
                "status": self.status, "variants": [item.to_dict() for item in self.variants],
                "selected": self.selected.to_dict() if self.selected else None,
                "layout": self.layout.to_dict() if self.layout else None,
                "error": self.error.to_dict() if self.error else None,
                "iframe_variant_count": self.iframe_variant_count,
                "provisional_layout": self.provisional_layout.provisional_dict() if self.provisional_layout else None,
                "quality_basis": "declared_resolution_frame_rate_codec_and_bitrate",
                "media_body_checked": False}


def _unsupported(message, code="UNSUPPORTED_HLS_LAYOUT"):
    return ArtworkError(code, message, "rendition")


def _resolved(base, reference, purpose):
    if not isinstance(reference, str) or not reference or "{" in reference or "}" in reference:
        raise _unsupported("HLS contains a missing URI or unsupported variable reference.")
    return validate_url(urljoin(base, reference), purpose)


def _parse(text, parser):
    if not isinstance(text, str) or not text.splitlines() or text.splitlines()[0] != "#EXTM3U":
        raise _unsupported("Response is not a valid HLS playlist.", "HLS_INVALID")
    if "\x00" in text:
        raise _unsupported("HLS playlist contains invalid text.", "HLS_INVALID")
    # m3u8 deliberately tolerates repeated attributes with last-value wins.
    # RFC 8216 forbids this; inspect before parsing so an encryption or range
    # declaration cannot be silently erased by a duplicate attribute.
    for line in text.splitlines():
        tag, separator, values = line.strip().partition(":")
        if tag not in _ATTRIBUTE_TAGS:
            continue
        seen, attributes = set(), []
        quoted = False
        start = 0
        for index, character in enumerate(values):
            if character == '"':
                quoted = not quoted
            elif character == "," and not quoted:
                attributes.append(values[start:index])
                start = index + 1
        attributes.append(values[start:])
        if not separator or quoted:
            raise _unsupported("HLS attribute list is malformed.", "HLS_INVALID")
        for attribute in attributes:
            name, equals, value = attribute.partition("=")
            # Observed Apple playlists add this ignored private attribute to
            # both variant tags. It does not replace AVERAGE-BANDWIDTH and
            # grants no exception to duplicate or encryption/range checks.
            private_bitrate = name == "_AVG-BANDWIDTH" and tag in {"#EXT-X-STREAM-INF", "#EXT-X-I-FRAME-STREAM-INF"}
            if (not equals or not value or (re.fullmatch(r"[A-Z0-9-]+", name) is None and not private_bitrate)
                    or name in seen or value.startswith("'")):
                raise _unsupported("HLS attribute list is malformed or contains duplicate attributes.", "HLS_INVALID")
            seen.add(name)
    try:
        return parser.loads(text)
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, AttributeError):
        raise _unsupported("HLS playlist could not be parsed.", "HLS_INVALID") from None


def _load_playlist(url, http, parser):
    parsed = None
    def validate_response(response):
        nonlocal parsed
        try:
            text = response.body.decode("utf-8", errors="strict")
        except UnicodeError:
            raise _unsupported("HLS playlist is not valid UTF-8.", "HLS_INVALID") from None
        parsed = _parse(text, parser)
    response = http.get(url, purpose="hls", max_bytes=MAX_PLAYLIST_BYTES,
                        validate_response=validate_response)
    if response.status != 200:
        raise ArtworkError("HLS_HTTP_ERROR", f"HLS request returned HTTP {response.status}.", "download")
    return parsed, response.final_url or url


def _is_positive_integer(value):
    return type(value) is int and value > 0


def _variants(master, base):
    if not master.is_variant or not master.playlists:
        raise _unsupported("Motion inspection requires a multivariant playlist with ordinary video variants.")
    if len(master.playlists) > MAX_VARIANTS:
        raise _unsupported("The HLS variant count exceeds the inspection limit.")
    variants = []
    for playlist in master.playlists:
        info = playlist.stream_info
        resolution = info.resolution
        width, height = resolution if resolution and len(resolution) == 2 else (None, None)
        frame_rate = info.frame_rate
        if frame_rate is not None:
            try:
                frame_rate = float(frame_rate)
            except (TypeError, ValueError, OverflowError):
                frame_rate = None
            if frame_rate is not None and (not math.isfinite(frame_rate) or frame_rate <= 0):
                frame_rate = None
        variants.append(MotionVariant(
            _resolved(base, playlist.uri, "hls"), width, height, info.codecs or "",
            frame_rate, info.average_bandwidth, info.bandwidth, info.video_range,
            info.stable_variant_id, info.audio, info.video))
    return tuple(variants)


def _select(variants, profile):
    candidates = tuple(item for item in variants if item.codec == "h264") if profile == "compatible" else variants
    if not candidates:
        raise _unsupported("No H.264 video variant is available for the compatible profile.", "HLS_PROFILE_UNAVAILABLE")
    for item in candidates:
        if item.codec not in {"h264", "hevc"} or item.audio_group or item.video_group:
            raise _unsupported("This HLS source needs unsupported codecs or alternate media groups.")
        if (not _is_positive_integer(item.width) or not _is_positive_integer(item.height)
                or item.frame_rate is None or not _is_positive_integer(item.bandwidth)
                or (item.average_bandwidth is not None and not _is_positive_integer(item.average_bandwidth))):
            raise _unsupported("Video variant dimensions, frame rate or bandwidth are missing or invalid.", "HLS_METADATA_INCOMPLETE")
    # The codec preference precedes bandwidth, so cross-codec bitrates are not
    # treated as a quality measurement. HDR metadata is preserved, not ranked.
    return max(candidates, key=lambda item: (
        item.width * item.height, item.frame_rate, item.codec == "hevc",
        item.average_bandwidth or item.bandwidth, item.bandwidth, item.url))


def _range(value, previous_end=None):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+(?:@[0-9]+)?", value) is None:
        raise _unsupported("Every initialization and media section must have a valid byte range.")
    length, separator, start = value.partition("@")
    if len(length) > 20 or len(start) > 20:
        raise _unsupported("HLS byte ranges exceed the supported size limit.", "RESPONSE_TOO_LARGE")
    length = int(length)
    offset = int(start) if separator else previous_end
    if length <= 0 or offset is None or offset < 0:
        raise _unsupported("HLS byte-range offsets or lengths are invalid.")
    return offset, length


def _layout(media, media_url, http):
    if media.is_variant or media.is_i_frames_only or not media.is_endlist or not media.segments:
        raise _unsupported("Only ended, ordinary video media playlists are supported.")
    if len(media.segments) > MAX_SEGMENTS:
        raise _unsupported("HLS segment count exceeds the inspection limit.")
    if media.skip or media.preload_hint or media.part_inf or media.rendition_reports or media.server_control:
        raise _unsupported("Delta updates and low-latency partial playlist declarations are unsupported.")
    if any(key is not None and key.method != "NONE" for key in media.keys) or media.session_keys:
        raise _unsupported("Encrypted HLS is unsupported; no keys are requested.", "HLS_ENCRYPTED")
    first_map = media.segments[0].init_section
    if first_map is None:
        raise _unsupported("This HLS media playlist has no initialization map.")
    resource = _resolved(media_url, first_map.uri, "motion")
    if not urlsplit(resource).path.lower().endswith(".mp4"):
        raise _unsupported("The HLS media resource must be an explicitly referenced MP4.")
    initial = _range(first_map.byterange)
    if initial[0] != 0:
        raise _unsupported("The initialization range must begin at byte zero.")
    ranges = [initial]
    end = sum(initial)
    durations = []
    for index, segment in enumerate(media.segments):
        if segment.discontinuity or segment.gap_tag or segment.parts:
            raise _unsupported("Discontinuities, gaps and partial HLS segments are unsupported.")
        if segment.init_section is None:
            raise _unsupported("Every segment must refer to the same initialization map.")
        current_map = segment.init_section
        if (_resolved(media_url, current_map.uri, "motion") != resource
                or _range(current_map.byterange) != initial
                or _resolved(media_url, segment.uri, "motion") != resource):
            raise _unsupported("This playlist does not describe one complete MP4 resource.")
        # An omitted offset refers to the previous media segment, never MAP.
        region = _range(segment.byterange, end if index else None)
        if region[0] != end:
            raise _unsupported("HLS byte ranges overlap, contain gaps, or cover only part of the file.")
        end = sum(region)
        if end > MAX_MOTION_BYTES:
            raise _unsupported("The declared video exceeds the 256 MiB limit.", "RESPONSE_TOO_LARGE")
        ranges.append(region)
        duration = segment.duration
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration) or duration <= 0:
            raise _unsupported("HLS segment duration is missing or invalid.", "HLS_INVALID")
        durations.append(duration)
    try:
        duration = math.fsum(durations)
    except OverflowError:
        raise _unsupported("HLS total duration exceeds the supported numeric range.", "HLS_INVALID") from None
    if not math.isfinite(duration) or duration <= 0:
        raise _unsupported("HLS total duration is invalid.", "HLS_INVALID")
    provisional = MotionLayout(resource, resource, media_url, end, duration, len(media.segments), tuple(ranges))
    try:
        return _confirm_layout(provisional, http)
    except ArtworkError as error:
        error.provisional_layout = provisional
        raise


def _confirm_layout(provisional, http):
    resource, end = provisional.source_mp4_url, provisional.content_length
    def validate_response(response):
        value = (_header(response.headers, "Content-Length") or "").strip()
        if re.fullmatch(r"[0-9]{1,18}", value) is None or int(value) != end:
            raise _unsupported("MP4 Content-Length is missing or differs from complete playlist coverage.")
        content_type = (_header(response.headers, "Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type not in {"video/mp4", "application/mp4", "application/octet-stream"}:
            raise _unsupported("The referenced media resource is not declared as MP4.")
        if (_header(response.headers, "Content-Encoding") or "identity").lower() != "identity":
            raise _unsupported("Compressed media transfer is unsupported.")
        validate_url(response.final_url or resource, "motion")
    response = http.head(resource, purpose="motion", validate_response=validate_response)
    if response.status != 200:
        raise ArtworkError("HLS_HEAD_FAILED", f"Media metadata request returned HTTP {response.status}.", "download")
    final_resource = response.final_url or resource
    return replace(provisional, mp4_url=final_resource)


_INCOMPLETE_HTTP_CODES = {"NETWORK_ERROR", "NETWORK_TIMEOUT", "HTTP_TRANSIENT_ERROR",
                          "RETRY_AFTER_DEADLINE", "REQUEST_LIMIT", "HLS_HTTP_ERROR",
                          "HLS_HEAD_FAILED", "CANCELLED"}


def _previous_inspection(value, asset, profile):
    """Restore only unfinished metadata work, preserving every known choice."""
    invalid = lambda: ArtworkError("RUN_STATE_INVALID", "Previous motion inspection is invalid or belongs to another frozen request.", "download")
    try:
        if isinstance(value, dict):
            raw_variants = value.get("variants")
            if not isinstance(raw_variants, list) or len(raw_variants) > MAX_VARIANTS or value.get("layout") is not None:
                raise invalid()
            fields = ("url", "width", "height", "codecs", "frame_rate", "average_bandwidth", "bandwidth",
                      "video_range", "stable_variant_id", "audio_group", "video_group")
            def variant(item):
                if not isinstance(item, dict):
                    raise invalid()
                result = MotionVariant(**{name: item.get(name) for name in fields})
                if "codec" in item and item["codec"] != result.codec:
                    raise invalid()
                return result
            variants = tuple(variant(item) for item in raw_variants)
            selected = variant(value["selected"]) if value.get("selected") is not None else None
            pending = value.get("provisional_layout")
            provisional = None
            if pending is not None:
                if (not isinstance(pending, dict) or pending.get("head_verified") is not False
                        or not isinstance(pending.get("ranges"), list)
                        or len(pending["ranges"]) > MAX_SEGMENTS + 1):
                    raise invalid()
                ranges = tuple((region["offset"], region["length"]) for region in pending["ranges"])
                provisional = MotionLayout(pending["source_mp4_url"], pending["source_mp4_url"],
                                           pending["media_url"], pending["content_length"], pending["duration"],
                                           pending["segment_count"], ranges)
            error = value.get("error")
            if not isinstance(error, dict) or type(error.get("retryable")) is not bool:
                raise invalid()
            previous_error = ArtworkError(error.get("code"), "Previous motion metadata request did not complete.",
                                           error.get("category"), error["retryable"], context=error.get("context"))
            value = MotionInspection(value.get("asset_id"), value.get("master_url"), value.get("profile"),
                                     value.get("status"), variants, selected, None, previous_error,
                                     value.get("resolved_master_url"), value.get("iframe_variant_count", 0), provisional)
        if (not isinstance(value, MotionInspection) or value.asset_id != asset.asset_id
                or value.master_url != asset.url or value.profile != profile or value.status != "failed"
                or value.layout is not None or not isinstance(value.error, ArtworkError)
                or value.error.category != "download" or value.error.code not in _INCOMPLETE_HTTP_CODES
                or type(value.iframe_variant_count) is not int or value.iframe_variant_count < 0
                or len(value.variants) > MAX_VARIANTS):
            raise invalid()
        validate_url(value.master_url, "hls")
        if value.selected is None:
            if value.variants or value.resolved_master_url is not None or value.provisional_layout is not None:
                raise invalid()
            return value
        validate_url(value.resolved_master_url, "hls")
        for item in value.variants:
            validate_url(item.url, "hls")
            if (item.frame_rate is not None and (isinstance(item.frame_rate, bool)
                    or not isinstance(item.frame_rate, (int, float))
                    or not math.isfinite(item.frame_rate) or item.frame_rate <= 0)):
                raise invalid()
        if value.selected not in value.variants or _select(value.variants, profile) != value.selected:
            raise invalid()
        pending = value.provisional_layout
        if pending is not None:
            validate_url(pending.media_url, "hls")
            validate_url(pending.source_mp4_url, "motion")
            if (pending.mp4_url != pending.source_mp4_url
                    or not urlsplit(pending.source_mp4_url).path.lower().endswith(".mp4")
                    or not _is_positive_integer(pending.content_length) or pending.content_length > MAX_MOTION_BYTES
                    or not _is_positive_integer(pending.segment_count)
                    or not 2 <= len(pending.ranges) <= MAX_SEGMENTS + 1
                    or pending.segment_count != len(pending.ranges) - 1
                    or isinstance(pending.duration, bool) or not isinstance(pending.duration, (int, float))
                    or not math.isfinite(pending.duration) or pending.duration <= 0):
                raise invalid()
            end = 0
            for offset, length in pending.ranges:
                if type(offset) is not int or offset != end or not _is_positive_integer(length):
                    raise invalid()
                end += length
            if end != pending.content_length:
                raise invalid()
        return value
    except (ArtworkError, AttributeError, KeyError, TypeError, ValueError, OverflowError):
        raise invalid() from None


def inspect_motion(asset: VisualAsset, http: HttpClient, profile: str = "best", *, previous=None) -> MotionInspection:
    """Fetch master/media text and one HEAD; never fetch media bytes or keys."""
    if profile not in {"best", "compatible"}:
        raise ArtworkError("INVALID_MOTION_PROFILE", "Choose best or compatible motion quality.", "input")
    if asset.kind != "video":
        raise ArtworkError("UNSUPPORTED_ASSET_KIND", "Motion inspection requires a video asset.", "rendition")
    parser = require_parser()
    result = (replace(_previous_inspection(previous, asset, profile), status="unsupported", error=None)
              if previous is not None else MotionInspection(asset.asset_id, asset.url, profile))
    try:
        if result.selected is None:
            master, master_url = _load_playlist(asset.url, http, parser)
            result = replace(result, resolved_master_url=master_url, iframe_variant_count=len(master.iframe_playlists))
            variants = _variants(master, master_url)
            result = replace(result, variants=variants)
            if master.session_keys or any(key is not None and key.method != "NONE" for key in master.keys):
                raise _unsupported("Encrypted HLS is unsupported; no keys are requested.", "HLS_ENCRYPTED")
            result = replace(result, selected=_select(variants, profile))
        if result.provisional_layout is not None:
            layout = _confirm_layout(result.provisional_layout, http)
        else:
            media, media_url = _load_playlist(result.selected.url, http, parser)
            layout = _layout(media, media_url, http)
        return replace(result, status="supported", layout=layout, provisional_layout=None)
    except ArtworkError as error:
        if getattr(error, "provisional_layout", None) is not None:
            result = replace(result, provisional_layout=error.provisional_layout)
        # HTTP 200 alone is not a successful HLS inspection. Invalid or
        # unsupported results do not seed a later command-local inspection.
        if callable(getattr(http, "forget_response", None)):
            for url in (asset.url, result.selected.url if result.selected else None):
                if url is not None:
                    http.forget_response(url, purpose="hls", max_bytes=MAX_PLAYLIST_BYTES)
        if error.category != "rendition":
            error.inspection = replace(result, status="failed", error=error)
            raise
        return replace(result, status="unsupported", error=error)
