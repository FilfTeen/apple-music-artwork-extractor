"""Preserve inspected MP4 bytes after local probing and complete decoding."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from fractions import Fraction
from hashlib import sha256
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import time

from .download import OutputCommitError, OutputDirectory
from .errors import ArtworkError
from .hls import MotionInspection
from .jsonio import loads_json


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)


class ProcessRunner:
    """Bound stdout/stderr while waiting; kill and reap only our own child."""

    def run(self, argv, *, timeout, max_output_bytes, control=None, on_stdout=None):
        if timeout <= 0 or max_output_bytes <= 0:
            raise ValueError("Invalid media process limits")
        if control is not None:
            control.check(category="validation")
        environment = dict(os.environ)
        environment.pop("FFREPORT", None)
        try:
            process = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=environment, shell=False)
        except OSError:
            raise ArtworkError("MEDIA_TOOL_UNAVAILABLE", "Unable to start the configured local media tool.", "download") from None
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        total = 0
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    if control is not None:
                        control.check(category="validation")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ArtworkError("MEDIA_TOOL_TIMEOUT", "Local media validation exceeded its time limit.", "validation")
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fd, min(65536, max_output_bytes - total + 1))
                        total += len(chunk)
                        if total > max_output_bytes:
                            raise ArtworkError("MEDIA_TOOL_OUTPUT_LIMIT", "Local media tool exceeded its diagnostic output limit.", "validation")
                        if chunk:
                            buffers[key.data].extend(chunk)
                            if key.data == "stdout" and on_stdout is not None:
                                on_stdout(chunk)
                        else:
                            selector.unregister(key.fileobj)
                while True:
                    if control is not None:
                        control.check(category="validation")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ArtworkError("MEDIA_TOOL_TIMEOUT", "Local media validation exceeded its time limit.", "validation")
                    try:
                        code = process.wait(timeout=min(remaining, 0.1))
                        break
                    except subprocess.TimeoutExpired:
                        continue
        finally:
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            process.wait()
            process.stdout.close()
            process.stderr.close()
        return ProcessResult(code, bytes(buffers["stdout"]), bytes(buffers["stderr"]))


@dataclass(frozen=True)
class MediaTools:
    ffmpeg: str = field(repr=False)
    ffprobe: str = field(repr=False)
    ffmpeg_version: str
    ffprobe_version: str
    runner: object = field(default_factory=ProcessRunner, repr=False, compare=False)

    @classmethod
    def discover(cls, *, ffmpeg=None, ffprobe=None, runner=None, control=None):
        runner = runner if runner is not None else ProcessRunner()
        paths, versions = {}, {}
        for name, supplied in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
            candidate = supplied if supplied is not None else shutil.which(name)
            if not candidate:
                raise ArtworkError("MEDIA_TOOL_MISSING", "Motion download requires local ffmpeg and ffprobe; install them or set APPLE_ARTWORK_FFMPEG and APPLE_ARTWORK_FFPROBE.", "download")
            path = Path(candidate)
            if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
                raise ArtworkError("MEDIA_TOOL_UNAVAILABLE", "Configured media tools must be executable files at absolute local paths.", "download")
            extra = {"control": control} if control is not None else {}
            result = runner.run([str(path), "-version"], timeout=10, max_output_bytes=65536, **extra)
            first_line = result.stdout.decode("utf-8", "replace").splitlines()
            match = re.match(rf"{name} version ([^\s]{{1,160}})(?:\s|$)", first_line[0]) if first_line else None
            if result.returncode != 0 or match is None:
                raise ArtworkError("MEDIA_TOOL_UNAVAILABLE", "A configured media tool did not return its expected version identity.", "download")
            paths[name], versions[name] = str(path), match.group(1)
        return cls(paths["ffmpeg"], paths["ffprobe"], versions["ffmpeg"], versions["ffprobe"], runner)

    def to_dict(self):
        return {"ffmpeg": {"name": "ffmpeg", "version": self.ffmpeg_version},
                "ffprobe": {"name": "ffprobe", "version": self.ffprobe_version}}


def motion_filename(asset, inspection):
    if not inspection.supported:
        raise inspection.error or ArtworkError("UNSUPPORTED_HLS_LAYOUT", "Motion source is not supported.", "rendition")
    identity = sha256(f"{inspection.layout.mp4_url}\n{inspection.layout.content_length}".encode()).hexdigest()[:16]
    return f"motion-{identity}.mp4"


def _positive_number(value):
    try:
        number = float(Fraction(str(value)))
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _packet_timeline(packets, stream):
    """Prove continuous decode and presentation intervals in integer ticks."""
    invalid = lambda: ArtworkError("VIDEO_TIMELINE_INVALID", "Video packet timestamps or durations cannot prove a continuous complete timeline.", "validation")
    if not isinstance(packets, list) or not packets or type(stream.get("index")) is not int:
        raise invalid()
    try:
        time_base = Fraction(str(stream.get("time_base")))
    except (ValueError, ZeroDivisionError, OverflowError):
        raise invalid() from None
    if time_base <= 0:
        raise invalid()
    presentation = []
    decode_start = decode_end = None
    durations = []
    for packet in packets:
        if (not isinstance(packet, dict) or type(packet.get("stream_index")) is not int
                or packet["stream_index"] != stream["index"]):
            raise invalid()
        if any(type(packet.get(name)) is not int or not -(2 ** 63) < packet[name] < 2 ** 63
               for name in ("pts", "dts", "duration")):
            raise invalid()
        pts, dts, duration = packet["pts"], packet["dts"], packet["duration"]
        flags = packet.get("flags", "")
        if duration <= 0 or not isinstance(flags, str) or "C" in flags or "D" in flags:
            raise invalid()
        if decode_end is not None and dts != decode_end:
            raise invalid()
        if decode_start is None:
            decode_start = dts
        decode_end = dts + duration
        presentation.append((pts, pts + duration))
        durations.append(duration)
    # B-frame packet order is decoding order. Presentation may go backward in
    # that order, so sort by PTS before checking every adjacent interval.
    presentation.sort()
    if any(previous[1] != current[0] for previous, current in zip(presentation, presentation[1:])):
        raise invalid()
    start, end = presentation[0][0], max(region[1] for region in presentation)
    span = (end - start) * time_base
    try:
        seconds = float(span)
    except OverflowError:
        raise invalid() from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise invalid()
    return {"time_base": str(time_base), "packet_count": len(packets),
            "presentation_start_tick": start, "presentation_end_tick": end,
            "decode_start_tick": decode_start, "decode_end_tick": decode_end,
            "minimum_packet_duration_tick": min(durations), "maximum_packet_duration_tick": max(durations),
            "presentation_coverage": "continuous", "decode_coverage": "continuous"}, span


class _DecodeProgress:
    """Read FFmpeg's documented progress lines without exposing diagnostics."""

    def __init__(self, control, item_id, total_seconds, attempt=None):
        self.control, self.item_id = control, item_id
        self.total_seconds, self.attempt = total_seconds, attempt
        self.pending = b""

    def __call__(self, chunk):
        self.pending += chunk
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            key, separator, value = line.strip().partition(b"=")
            if separator and key == b"out_time_us":
                try:
                    seconds = int(value) / 1_000_000
                except (ValueError, OverflowError):
                    continue
                if math.isfinite(seconds) and seconds >= 0:
                    event = {"kind": "decode", "item_id": self.item_id,
                             "processed_seconds": seconds, "total_seconds": self.total_seconds}
                    if self.attempt is not None:
                        event["attempt"] = self.attempt
                    self.control.emit(event)


def validate_motion(path, inspection, tools, transfer, *, control=None, item_id=None, attempt=None):
    """Only local owned files enter the tools; tool diagnostics stay private."""
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ArtworkError("UNSAFE_OUTPUT_PATH", "Video validation requires an owned local temporary file.", "validation")
    selected, layout = inspection.selected, inspection.layout
    if not inspection.supported or selected is None:
        raise ArtworkError("UNSUPPORTED_HLS_LAYOUT", "Motion must be inspected before validation.", "rendition")
    if control is not None:
        control.check(category="validation")
    process_options = {"control": control} if control is not None else {}
    probe = tools.runner.run([
        tools.ffprobe, "-v", "error", "-protocol_whitelist", "file",
        "-show_packets", "-show_entries", "format=format_name,start_time,duration,size:stream=index,codec_name,codec_type,width,height,avg_frame_rate,r_frame_rate,start_pts,start_time,duration_ts,duration,time_base,pix_fmt,color_range,color_space,color_transfer,color_primaries:stream_disposition=attached_pic:packet=stream_index,pts,dts,duration,flags",
        "-of", "json", "-i", str(path)], timeout=30, max_output_bytes=1024 * 1024, **process_options)
    if probe.returncode != 0:
        raise ArtworkError("VIDEO_PROBE_FAILED", "The local media probe could not read the complete MP4.", "validation")
    try:
        metadata = loads_json(probe.stdout)
    except ArtworkError:
        raise ArtworkError("VIDEO_PROBE_FAILED", "The local media probe returned invalid metadata.", "validation") from None
    if not isinstance(metadata, dict):
        raise ArtworkError("VIDEO_PROBE_FAILED", "The local media probe returned invalid metadata.", "validation")
    streams, container = metadata.get("streams"), metadata.get("format")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict):
        raise ArtworkError("VIDEO_TRACK_MISMATCH", "Supported artwork must contain exactly one video track and no additional tracks.", "validation")
    stream = streams[0]
    if stream.get("codec_type") != "video" or stream.get("disposition", {}).get("attached_pic", 0):
        raise ArtworkError("VIDEO_TRACK_MISMATCH", "Artwork does not contain the expected ordinary video track.", "validation")
    if not isinstance(container, dict) or "mp4" not in str(container.get("format_name", "")).split(","):
        raise ArtworkError("MEDIA_TYPE_MISMATCH", "Decoded media is not an MP4 container.", "validation")
    try:
        actual_size = int(container.get("size", ""))
    except (TypeError, ValueError, OverflowError):
        actual_size = None
    if actual_size != layout.content_length or path.stat().st_size != layout.content_length or transfer["bytes"] != layout.content_length:
        raise ArtworkError("DOWNLOAD_LENGTH_MISMATCH", "Validated file length differs from the inspected MP4.", "validation")
    if stream.get("codec_name") != selected.codec:
        raise ArtworkError("VIDEO_CODEC_MISMATCH", "Actual video codec differs from the selected HLS variant.", "validation")
    width, height = stream.get("width"), stream.get("height")
    if type(width) is not int or type(height) is not int or (width, height) != (selected.width, selected.height):
        raise ArtworkError("ASSET_DIMENSION_MISMATCH", "Actual video dimensions differ from the selected HLS variant.", "validation")
    average = _positive_number(stream.get("avg_frame_rate"))
    nominal = _positive_number(stream.get("r_frame_rate"))
    # HLS FRAME-RATE declares a maximum, not an exact average for VFR video.
    if average is None or nominal is None or max(average, nominal) > selected.frame_rate + 0.05:
        raise ArtworkError("VIDEO_FRAME_RATE_MISMATCH", "Actual frame rate is invalid or exceeds the HLS declaration.", "validation")
    timeline, presentation_span = _packet_timeline(metadata.get("packets"), stream)
    duration = float(presentation_span)
    tolerance = max(0.1, 2 / average)
    if abs(duration - layout.duration) > tolerance:
        raise ArtworkError("VIDEO_DURATION_MISMATCH", "Actual packet presentation span differs from the complete HLS timeline.", "validation")
    if control is not None:
        control.check(category="validation")
        process_options["on_stdout"] = _DecodeProgress(control, item_id, duration, attempt)
    decoded = tools.runner.run([
        tools.ffmpeg, "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file",
        "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn", "-progress", "pipe:1", "-f", "null", "-"],
        timeout=300, max_output_bytes=1024 * 1024, **process_options)
    progress = dict(line.split("=", 1) for line in decoded.stdout.decode("ascii", "replace").splitlines() if "=" in line)
    try:
        frames = int(progress.get("frame", "0"))
    except ValueError:
        frames = 0
    if decoded.returncode != 0 or progress.get("progress") != "end" or frames <= 0:
        raise ArtworkError("VIDEO_DECODE_FAILED", "Video did not pass complete local decoding.", "validation")
    try:
        decode_endpoint = Fraction(int(progress["out_time_us"]), 1_000_000)
        duplicated, dropped = int(progress["dup_frames"]), int(progress["drop_frames"])
    except (KeyError, ValueError, OverflowError):
        raise ArtworkError("VIDEO_DECODE_TIMELINE_MISMATCH", "Full-decode timing evidence is missing or invalid.", "validation") from None
    if (frames != timeline["packet_count"] or duplicated != 0 or dropped != 0 or decode_endpoint <= 0
            or abs(float(decode_endpoint - presentation_span)) > tolerance):
        raise ArtworkError("VIDEO_DECODE_TIMELINE_MISMATCH", "Complete decoding did not preserve the verified frame count and presentation span.", "validation")
    return {"actual_dimensions": {"width": width, "height": height}, "actual_format": "MP4",
            "actual_codec": stream["codec_name"], "frame_rate": {"average": average, "nominal": nominal,
                                                                         "declared_maximum": selected.frame_rate},
            "duration_seconds": duration, "duration_basis": "complete_packet_presentation_span",
            "duration_tolerance_seconds": tolerance, "packet_timeline": timeline,
            "timing_declarations": {"container": {key: container.get(key) for key in ("start_time", "duration")},
                                    "stream": {key: stream.get(key) for key in ("start_pts", "start_time", "duration_ts", "duration")}},
            "decode_endpoint_seconds": float(decode_endpoint), "track_count": 1,
            "pixel_format": stream.get("pix_fmt"),
            "color": {key: stream.get(key) for key in ("color_range", "color_space", "color_transfer", "color_primaries")},
            "bytes": transfer["bytes"], "sha256": transfer["sha256"], "decoded": True,
            "full_decode": True, "decoded_frames": frames, "transcoded": False}


def prepare_motion(asset, inspection: MotionInspection, temporary, http, media_tools, *,
                   control=None, item_id=None, deadline=None, validation_gate=None):
    """Write and validate one coordinator-owned temporary; never publish it."""
    control = control if control is not None else getattr(http, "control", None)
    item_id = item_id if item_id is not None else getattr(http, "item_id", None)
    attempt = getattr(http, "attempt", None)
    filename = motion_filename(asset, inspection)
    if temporary.final_name != filename:
        raise ArtworkError("OUTPUT_IDENTITY_CHANGED", "Allocated video destination differs from the frozen request.", "download")
    artifact = {"asset_id": asset.asset_id, "source_identity": asset.source_identity,
                "semantic_path": asset.semantic_path, "path": filename, "kind": "video",
                "master_url": asset.url, "url": inspection.layout.mp4_url,
                "profile": inspection.profile, "selected_variant": inspection.selected.to_dict(),
                "layout": inspection.layout.to_dict(), "status": "downloaded"}

    def stage(name):
        if control is not None:
            event = {"kind": "stage", "item_id": item_id, "stage": name}
            if attempt is not None:
                event["attempt"] = attempt
            control.emit(event)

    def writer(stream):
        if control is not None:
            control.check(deadline)
        stage("download")
        transfer = http.stream_to(inspection.layout.mp4_url, stream,
                                  expected_bytes=inspection.layout.content_length,
                                  max_attempts=1, deadline=deadline)
        artifact["url"] = transfer["url"]
        return transfer

    def validator(path, transfer):
        stage("waiting_validation")
        with validation_gate if validation_gate is not None else nullcontext():
            stage("validate")
            artifact["validation"] = validate_motion(path, inspection, media_tools, transfer,
                                                      control=control, item_id=item_id, attempt=attempt)
        return artifact["validation"]

    temporary.write_stream(writer, validator)
    return artifact


def download_motion(asset, inspection: MotionInspection, output: OutputDirectory, http, media_tools):
    filename = motion_filename(asset, inspection)
    output.ensure_available([filename])
    artifact = {"asset_id": asset.asset_id, "source_identity": asset.source_identity,
                "semantic_path": asset.semantic_path, "path": filename, "kind": "video",
                "master_url": asset.url, "url": inspection.layout.mp4_url,
                "profile": inspection.profile, "selected_variant": inspection.selected.to_dict(),
                "layout": inspection.layout.to_dict(), "status": "downloaded"}

    def writer(stream):
        transfer = http.stream_to(inspection.layout.mp4_url, stream, expected_bytes=inspection.layout.content_length)
        artifact["url"] = transfer["url"]
        return transfer

    def validator(path, transfer):
        artifact["validation"] = validate_motion(path, inspection, media_tools, transfer)
        return artifact["validation"]

    try:
        output.write_stream(filename, writer, validator)
    except OutputCommitError as error:
        if error.committed:
            error.artifact = {**artifact, "output": error.output_state(), "cleanup_error": error.to_dict()}
        raise
    return artifact
