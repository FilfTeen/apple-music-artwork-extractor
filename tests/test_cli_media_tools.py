"""Explicit installed-CLI acceptance using owned MP4s and real media tools.

The first sample is 64x64, two frames at 2 fps. The second is 64x64, 48
frames at 24 fps, with B-frame reordering and a nonzero fragmented-MP4
start. CI generates both; no Apple media binary belongs in the package.
"""

import argparse
from fractions import Fraction
from hashlib import sha256
from importlib.metadata import distribution
from copy import deepcopy
from io import BytesIO, StringIO
import json
import shutil
from pathlib import Path
import subprocess
import tempfile
from threading import Lock

from PIL import Image

import apple_music_artwork
from apple_music_artwork.cli import main
from apple_music_artwork.client import HttpResponse
from apple_music_artwork.motion import MediaTools, ProcessRunner
from apple_music_artwork.jsonio import dumps_json
from apple_music_artwork.run_state import fingerprint


BASE = "https://mvod.itunes.apple.com/itunes-assets/synthetic-integration/"
IMAGE_URL = "https://is1-ssl.mzstatic.com/image/thumb/synthetic-integration/{w}x{h}bb.{f}"


def _safe_path(path):
    path = path.absolute()
    for ancestor in (path, *path.parents):
        assert not ancestor.is_symlink(), "test paths must not traverse symlinks"
    return path


def _boxes(body, start=0, stop=None):
    stop = len(body) if stop is None else stop
    position = start
    while position < stop:
        assert position + 8 <= stop, "truncated test input box header"
        size = int.from_bytes(body[position:position + 4], "big")
        kind, header = body[position + 4:position + 8], 8
        if size == 1:
            assert position + 16 <= stop, "truncated test input box size"
            size, header = int.from_bytes(body[position + 8:position + 16], "big"), 16
        if size == 0:
            size = stop - position
        assert size >= header and position + size <= stop, "invalid test input box"
        yield position, size, kind, header
        position += size


def _media_offset(body):
    metadata = set()
    for position, size, kind, header in _boxes(body):
        if kind in {b"moof", b"mdat"}:
            assert {b"ftyp", b"moov"} <= metadata and position > 0, "input needs metadata before media"
            return position
        metadata.add(kind)
    raise AssertionError("input has no media box")


def _shift_fragment_timestamps(body, offset_ticks):
    """Shift owned fMP4 tfdt fields; preserve every compressed media byte."""
    shifted, count = bytearray(body), 0
    boxes = list(_boxes(body))
    for position, size, kind, header in boxes:
        if kind != b"moof":
            continue
        for traf, traf_size, traf_kind, traf_header in _boxes(body, position + header, position + size):
            if traf_kind != b"traf":
                continue
            for atom, atom_size, atom_kind, atom_header in _boxes(body, traf + traf_header, traf + traf_size):
                if atom_kind != b"tfdt":
                    continue
                full_box = atom + atom_header
                version = body[full_box]
                assert version in {0, 1}, "unsupported owned tfdt version"
                width, field = 8 if version == 1 else 4, full_box + 4
                assert field + width == atom + atom_size, "unexpected tfdt shape"
                old = int.from_bytes(body[field:field + width], "big")
                shifted[field:field + width] = (old + offset_ticks).to_bytes(width, "big")
                count += 1
    assert count > 0, "the generated file must be fragmented"
    # Optional mfra indexes still describe the old timeline; omit only this
    # terminal index. Actual moov/moof/mdat contents remain complete.
    if boxes[-1][2] == b"mfra":
        shifted = shifted[:boxes[-1][0]]
    shifted = bytes(shifted)
    media = lambda data: [data[p + header:p + size] for p, size, kind, header in _boxes(data) if kind == b"mdat"]
    assert media(body) == media(shifted), "media payload changed during fixture preparation"
    return shifted


def generate_owned_inputs(root, ffmpeg, ffprobe):
    """CI-only generation; the host may supply separately owned input files."""
    root = _safe_path(Path(root))
    assert root.is_dir()
    small, bframes = root / "owned-two-frames.mp4", root / "owned-bframes-before-offset.mp4"
    shifted = root / "owned-offset-bframes.mp4"
    common = [str(ffmpeg), "-nostdin", "-v", "error", "-n", "-f", "rawvideo", "-pixel_format", "yuv420p",
              "-video_size", "64x64"]
    encode = ["-an", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    fragment = ["-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-f", "mp4"]
    frame = bytes([64]) * 4096 + bytes([96]) * 1024 + bytes([160]) * 1024
    subprocess.run([*common, "-framerate", "2", "-i", "pipe:0", "-frames:v", "2", *encode,
                    "-bf", "0", *fragment, str(small)], input=frame * 2, check=True, timeout=60)
    frames = b"".join(bytes([32 + number * 3]) * 4096 + bytes([80 + number]) * 1024
                      + bytes([170 - number]) * 1024 for number in range(48))
    subprocess.run([*common, "-framerate", "24", "-i", "pipe:0", "-frames:v", "48", *encode,
                    "-bf", "2", "-g", "24", "-b_strategy", "0", "-sc_threshold", "0", *fragment, str(bframes)],
                   input=frames, check=True, timeout=60)
    observed = subprocess.run([str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_streams",
                               "-of", "json", str(bframes)], capture_output=True, check=True, timeout=30)
    time_base = Fraction(json.loads(observed.stdout)["streams"][0]["time_base"])
    offset = Fraction(10) / time_base
    assert offset.denominator == 1
    with shifted.open("xb") as stream:
        stream.write(_shift_fragment_timestamps(bframes.read_bytes(), int(offset)))
    return small, shifted


class _Stream(BytesIO):
    status = 200

    def __init__(self, body):
        super().__init__(body)
        self.headers = {"Content-Type": "video/mp4", "Content-Length": str(len(body))}


class _Transport:
    """Explicit routes, with independent transport objects sharing an audit log."""

    def __init__(self, body, *, fps, duration):
        self.body, self.calls, self.lock = body, [], Lock()
        split = _media_offset(body)
        artwork = {"url": IMAGE_URL, "width": 64, "height": 64, "hasP3": False}
        attributes = {"artwork": artwork, "editorialArtwork": {"staticDetailSquare": artwork},
                      "editorialVideo": {"motionDetailSquare": {"video": BASE + "master.m3u8"},
                                         "motionSquareVideo1x1": {"video": BASE + "master.m3u8"}}}
        self.metadata = {"data": [{"id": "1", "type": "albums", "href": "/v1/catalog/us/albums/1", "attributes": attributes}]}
        self.texts = {
            BASE + "master.m3u8": f'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=64x64,FRAME-RATE={fps},CODECS="avc1.64000A"\nmedia.m3u8\n',
            BASE + "media.m3u8": f'#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:{int(duration)}\n#EXT-X-MAP:URI="movie.mp4",BYTERANGE="{split}@0"\n#EXTINF:{duration},\n#EXT-X-BYTERANGE:{len(body) - split}@{split}\nmovie.mp4\n#EXT-X-ENDLIST\n',
        }
        image = BytesIO()
        with Image.new("RGBA", (64, 64), (31, 62, 93, 128)) as pixels:
            pixels.save(image, format="PNG")
        self.png = image.getvalue()

    def fork(self):
        worker = object.__new__(type(self))
        worker.__dict__ = dict(self.__dict__)
        return worker

    def _record(self, method, url):
        with self.lock:
            self.calls.append((method, url))

    def request(self, url, **kwargs):
        self._record("GET", url)
        if url == "https://api.music.apple.com/v1/catalog/us/albums/1":
            return HttpResponse(200, {"Content-Type": "application/json"}, json.dumps(self.metadata).encode())
        if url in self.texts:
            return HttpResponse(200, {"Content-Type": "application/vnd.apple.mpegurl"}, self.texts[url].encode())
        if url == IMAGE_URL.format(w=64, h=64, f="png"):
            return HttpResponse(200, {"Content-Type": "image/png"}, self.png)
        raise AssertionError("Unexpected synthetic GET route")

    def head(self, url, **kwargs):
        assert url == BASE + "movie.mp4"
        self._record("HEAD", url)
        return HttpResponse(200, {"Content-Type": "video/mp4", "Content-Length": str(len(self.body))}, b"")

    def stream(self, url, **kwargs):
        assert url == BASE + "movie.mp4"
        self._record("STREAM", url)
        return _Stream(self.body)


class _NoNetwork:
    def request(self, *args, **kwargs):
        raise AssertionError("Completed-directory reuse attempted a network request")
    head = stream = request

    def fork(self):
        return _NoNetwork()


class _AuditedRunner(ProcessRunner):
    def __init__(self):
        self.operations = []

    def run(self, argv, **kwargs):
        self.operations.append("version" if "-version" in argv else "probe" if "-show_packets" in argv else "decode")
        return super().run(argv, **kwargs)


def _input_evidence(path, tools, *, bframes):
    path = _safe_path(path)
    body = path.read_bytes()
    assert 0 < len(body) <= 1024 * 1024, "the integration input must stay small"
    probed = subprocess.run([tools.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_streams",
                             "-show_format", "-show_packets", "-of", "json", str(path)],
                            capture_output=True, check=True, timeout=30)
    result = json.loads(probed.stdout)
    assert len(result["streams"]) == 1
    stream, packets = result["streams"][0], result["packets"]
    frames, fps, duration = (48, 24, 2) if bframes else (2, 2, 1)
    assert stream["codec_name"] == "h264" and (stream["width"], stream["height"]) == (64, 64)
    assert len(packets) == frames and Fraction(stream["avg_frame_rate"]) == fps
    time_base = Fraction(stream["time_base"])
    pts = [int(packet["pts"]) for packet in packets]
    dts = [int(packet["dts"]) for packet in packets]
    durations = [int(packet["duration"]) for packet in packets]
    assert all(value > 0 for value in durations)
    intervals = sorted(zip(pts, durations))
    assert all(start + length == following[0] for (start, length), following in zip(intervals, intervals[1:]))
    assert all(start + length == following for start, length, following in zip(dts, durations, dts[1:]))
    span = (max(start + length for start, length in intervals) - min(pts)) * time_base
    assert span == duration
    if bframes:
        assert stream["has_b_frames"] > 0 and pts != sorted(pts)
        assert min(pts) * time_base > 0
        assert abs(Fraction(result["format"]["duration"]) - span) > 5
        pictures = subprocess.run([tools.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
                                   "-show_entries", "frame=pict_type", "-of", "json", str(path)],
                                  capture_output=True, check=True, timeout=30)
        assert any(frame["pict_type"] == "B" for frame in json.loads(pictures.stdout)["frames"])
    peak = 1 / (min(durations) * time_base)
    assert peak == fps, "owned constant-rate input differs from its generation contract"
    last_start = (max(pts) - min(pts)) * time_base
    assert last_start == (Fraction(47, 24) if bframes else Fraction(1, 2))
    return body, {"frames": frames, "fps": fps, "duration": duration, "has_b_frames": stream["has_b_frames"],
                  "average_frame_rate": stream["avg_frame_rate"], "nominal_frame_rate": stream["r_frame_rate"],
                  "observed_maximum_fps": float(peak), "time_base": str(time_base),
                  "last_frame_start_relative_seconds": float(last_start),
                  "presentation_start_seconds": float(min(pts) * time_base),
                  "container_duration_seconds": float(result["format"]["duration"])}


def _invoke(output, transport, tools):
    stdout, stderr = StringIO(), StringIO()
    code = main(["https://music.apple.com/us/album/example/1", "--provider", "catalog", "--all-media",
                 "--output", str(output), "--json"], transport=transport, media_tools=tools,
                environ={"APPLE_MUSIC_DEVELOPER_TOKEN": "synthetic-test-value"}, stdout=stdout, stderr=stderr)
    assert stderr.getvalue() == ""
    result = json.loads(stdout.getvalue())
    assert str(output) not in stdout.getvalue()
    return code, result


def _exercise(root, body, evidence, tools):
    output = Path(tempfile.mkdtemp(prefix="cli-real-tools-", dir=root))
    transport = _Transport(body, fps=evidence["fps"], duration=evidence["duration"])
    code, result = _invoke(output, transport, tools)
    assert code == 0 and result["status"] == "success", result.get("errors")
    assert result["schema_version"] == 2 and result["run"]["jobs"] == 2
    assert result["manifest_report"]["status"] == "saved"
    assert result["coverage"]["completeness"]["status"] == "complete"
    assert len(result["downloads"]) == 2 and all(item["action"] == "downloaded" for item in result["downloads"])
    video = next(item for item in result["downloads"] if item["kind"] == "video")
    image = next(item for item in result["downloads"] if item["kind"] == "image")
    assert len(video["references"]) == len(image["references"]) == 2
    assert (output / video["path"]).read_bytes() == body
    validation = video["validation"]
    assert validation["actual_codec"] == "h264" and validation["actual_dimensions"] == {"width": 64, "height": 64}
    assert validation["full_decode"] is True and validation["decoded_frames"] == evidence["frames"]
    assert validation["transcoded"] is False and abs(validation["duration_seconds"] - evidence["duration"]) < .01
    assert validation["frame_rate"]["observed_maximum"] == evidence["observed_maximum_fps"]
    assert validation["duration_basis"] == "complete_packet_presentation_span"
    assert validation["packet_timeline"]["packet_count"] == evidence["frames"]
    # FFmpeg versions report either the last frame start or the full span.
    endpoint = validation["decode_endpoint_seconds"]
    assert min(abs(endpoint - value) for value in
               (evidence["last_frame_start_relative_seconds"], evidence["duration"])) <= 0.000001
    assert validation["bytes"] == len(body) and validation["sha256"] == sha256(body).hexdigest()
    assert (output / image["path"]).read_bytes() == transport.png and image["validation"]["actual_format"] == "PNG"
    with Image.open(output / image["path"]) as pixels:
        pixels.load()
        assert pixels.size == (64, 64) and pixels.mode == "RGBA"
    saved = json.loads((output / "manifest.json").read_text())
    assert saved == {name: value for name, value in result.items() if name != "manifest_report"}
    assert saved["status_scope"] == "download_results"
    assert "manifest_report" not in saved and not list(output.glob("*.part"))
    assert len(transport.calls) == 6 and sum(method == "STREAM" for method, _ in transport.calls) == 1
    assert result["run"]["stats"]["requests"] == len(transport.calls)
    before = list(tools.runner.operations)
    code, reused = _invoke(output, _NoNetwork(), tools)
    assert code == 0 and reused["run"]["resumed"] is True, reused.get("errors")
    assert all(reused["run"]["stats"][name] == 0 for name in ("requests", "received_bytes", "cache_hits"))
    assert reused["run"]["counts"]["new_downloaded"] == 0 and reused["run"]["counts"]["reused"] == 2
    assert all(item["action"] == "reused" and item.get("local_verification") for item in reused["downloads"])
    assert [item["validation"] for item in reused["downloads"]] == [item["validation"] for item in result["downloads"]]
    assert tools.runner.operations == before, "reuse unexpectedly probed or decoded the media again"
    saved_reuse = json.loads((output / "manifest.json").read_text())
    assert saved_reuse == {name: value for name, value in reused.items() if name != "manifest_report"}
    policy_upgrade = _old_policy_recovery(output, video, image, body, evidence, tools)
    return {"output": str(output), "video_bytes": len(body), "video_sha256": sha256(body).hexdigest(),
            "old_policy_recovery": policy_upgrade,
            "decoded_frames": validation["decoded_frames"], "input_evidence": evidence,
            "http_operations": len(transport.calls), "reuse_requests": 0, "reuse_media_processes": 0}


def _old_policy_recovery(source_output, video, image, body, evidence, tools):
    """Synthetic v1-policy state, not an artifact created by an older wheel."""
    output = Path(tempfile.mkdtemp(prefix="cli-old-policy-", dir=source_output.parent))
    shutil.copytree(source_output, output, dirs_exist_ok=True)
    state_path = output / ".apple-artwork-state" / "state.json"
    state = json.loads(state_path.read_text())
    item_id, = [key for key, record in state["payload"]["files"].items()
                if record["artifact"]["kind"] == "video"]
    record = state["payload"]["files"][item_id]
    record["validation_policy"] = "mp4-packet-full-decode-v1"
    record.pop("local_verification", None)
    record["artifact"]["validation"]["frame_rate"].pop("observed_maximum", None)
    legacy_artifact = deepcopy(record["artifact"])
    state["payload_sha256"] = fingerprint(state["payload"])
    state_path.write_text(dumps_json(state), encoding="utf-8")
    before = len(tools.runner.operations)
    code, first = _invoke(output, _NoNetwork(), tools)
    assert code == 0, first.get("errors")
    assert first["run"]["stats"]["requests"] == first["run"]["stats"]["received_bytes"] == 0
    operations = tools.runner.operations[before:]
    assert operations.count("probe") == operations.count("decode") == 1, operations
    upgraded = next(item for item in first["downloads"] if item["kind"] == "video")
    local = upgraded["local_verification"]
    assert upgraded["validation"] == legacy_artifact["validation"]
    assert local["decoded_this_run"] is True and local["method"] == "full_local_validation"
    assert local["validation_policy"] == "mp4-packet-full-decode-v2"
    assert local["validation"]["frame_rate"]["observed_maximum"] == evidence["fps"]
    assert local["validation"]["full_decode"] is True
    assert local["validation"]["decoded_frames"] == evidence["frames"]
    assert (output / video["path"]).read_bytes() == body
    after_first = json.loads(state_path.read_text())["payload"]["files"][item_id]
    assert after_first["artifact"] == legacy_artifact
    assert after_first["local_verification"] == local
    before = list(tools.runner.operations)
    code, second = _invoke(output, _NoNetwork(), tools)
    assert code == 0 and second["run"]["stats"]["requests"] == 0, second.get("errors")
    assert tools.runner.operations == before, "second resume must not start media processes"
    repeated = next(item for item in second["downloads"] if item["kind"] == "video")
    assert repeated["validation"] == legacy_artifact["validation"]
    assert repeated["local_verification"]["decoded_this_run"] is False
    assert repeated["local_verification"]["previous_full_validation"] == local
    # A later changed file is retained and rejected before media tools run.
    damaged = bytes([body[0] ^ 1]) + body[1:]
    (output / video["path"]).write_bytes(damaged)
    code, rejected = _invoke(output, _NoNetwork(), tools)
    assert code != 0 and rejected["run"]["stats"]["requests"] == 0
    assert tools.runner.operations == before
    failed = next(item for item in rejected["downloads"] if item["kind"] == "video")
    assert failed["error"]["code"] == "OUTPUT_CONFLICT"
    assert (output / video["path"]).read_bytes() == damaged
    assert (output / image["path"]).is_file()
    retained = json.loads(state_path.read_text())["payload"]["files"][item_id]
    assert retained["artifact"] == legacy_artifact
    assert retained["local_verification"]["previous_full_validation"] == local
    return {"fixture_kind": "synthetic_v1_policy_state", "first_resume_requests": 0,
            "first_resume_probes": 1, "first_resume_full_decodes": 1,
            "second_resume_requests": 0, "second_resume_media_processes": 0,
            "original_validation_preserved": True, "current_validation_retained": True,
            "damaged_file": "preserved_and_rejected_without_network_or_media_processes"}


def _truncation(root, body, evidence, tools):
    output = Path(tempfile.mkdtemp(prefix="cli-truncated-tools-", dir=root))
    truncated = body[:len(body) * 3 // 4]
    # Advertise the truncated object's actual length so that transport and
    # HEAD both pass. Only real media inspection/decoding can reject it.
    transport = _Transport(truncated, fps=evidence["fps"], duration=evidence["duration"])
    code, result = _invoke(output, transport, tools)
    assert code == 8 and result["status"] == "partial", result.get("errors")
    assert result["control"]["lifecycle"] == "needs_action" and result["control"]["decision_required"] is True
    failed = next(item for item in result["downloads"] if item["kind"] == "video")
    image = next(item for item in result["downloads"] if item["kind"] == "image")
    assert failed["status"] == "failed" and failed["error"]["category"] == "validation"
    assert image["status"] == "downloaded" and (output / image["path"]).is_file()
    assert not list(output.rglob("*.mp4")) and not list(output.rglob("*.part"))
    assert sum(method == "STREAM" for method, _ in transport.calls) == 1, "validation errors must not retry"
    assert result["manifest_report"]["status"] == "saved"
    assert json.loads((output / "manifest.json").read_text()) == {
        name: value for name, value in result.items() if name != "manifest_report"}
    return {"output": str(output), "status": "rejected", "error_code": failed["error"]["code"],
            "received_bytes": len(truncated), "image_preserved": True}


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-video", required=True, type=Path)
    parser.add_argument("--bframes-video", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    parser.add_argument("--require-installed", action="store_true")
    args = parser.parse_args(argv)
    package = Path(apple_music_artwork.__file__).resolve()
    if args.require_installed:
        assert not package.is_relative_to(Path(__file__).resolve().parents[1] / "src"), "source package was imported"
        installed = distribution("apple-music-artwork")
        assert package == Path(installed.locate_file("apple_music_artwork/__init__.py")).resolve()
        assert apple_music_artwork.__version__ == installed.version
    root = _safe_path(args.output_root)
    assert root.is_dir(), "create a dedicated output root first"
    tools = MediaTools.discover(ffmpeg=args.ffmpeg, ffprobe=args.ffprobe, runner=_AuditedRunner())
    results = []
    for path, bframes in ((args.input_video, False), (args.bframes_video, True)):
        body, evidence = _input_evidence(path, tools, bframes=bframes)
        results.append(_exercise(root, body, evidence, tools))
        if bframes:
            truncated = _truncation(root, body, evidence, tools)
    summary = {"status": "passed", "version": apple_music_artwork.__version__, "package_file": str(package),
               "samples": results, "truncation": truncated, "media_tools": tools.to_dict(), "network_used": False}
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
