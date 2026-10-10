"""Decode before committing; publish each file without replacing existing data."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import re
import stat
from uuid import uuid4
import warnings

from PIL import Image, UnidentifiedImageError

from .client import HttpClient, MAX_IMAGE_BYTES
from .errors import ArtworkError
from .models import ImageRequest, VisualAsset


MAX_IMAGE_PIXELS = 40_000_000
_FORMATS = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "avif": "AVIF"}
_MIME = {"JPEG": {"image/jpeg", "image/jpg"}, "PNG": {"image/png"},
         "WEBP": {"image/webp"}, "AVIF": {"image/avif"}}
_TEMPORARY_NAME = re.compile(r"\.apple-artwork-[0-9a-f]{32}\.part")


class OutputCommitError(ArtworkError):
    """A write failed, with an explicit account of any committed file/residue."""

    def __init__(self, code, message, *, path, committed=False, residue=(), category="download", retryable=False, context=None):
        super().__init__(code, message, category, retryable, context=context)
        self.path = path
        self.committed = committed
        self.residue = tuple(residue)
        self.artifact = None

    def output_state(self):
        return {"path": self.path, "committed": self.committed, "residue": list(self.residue)}


def _identity(info):
    return {"device": info.st_dev, "inode": info.st_ino,
            "size": info.st_size, "mtime_ns": info.st_mtime_ns}


@dataclass
class OwnedTemporary:
    """A coordinator-allocated file; workers may write and validate, never publish."""

    directory: "OutputDirectory" = field(repr=False)
    name: str
    final_name: str
    device: int
    inode: int
    sealed: dict | None = field(default=None, repr=False)

    @property
    def path(self):
        self.directory._name(self.final_name)
        return self.directory._absolute / self.name

    def _inspect(self, *, sealed=False):
        try:
            info = os.stat(self.name, dir_fd=self.directory._fd, follow_symlinks=False)
            local = self.path.lstat()
        except OSError:
            raise ArtworkError("OUTPUT_IDENTITY_CHANGED", "Registered temporary output is unavailable.", "download") from None
        expected = (self.device, self.inode)
        if (not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(local.st_mode)
                or (info.st_dev, info.st_ino) != expected or (local.st_dev, local.st_ino) != expected):
            raise ArtworkError("OUTPUT_IDENTITY_CHANGED", "Registered temporary output identity changed.", "download")
        current = _identity(info)
        if sealed and (self.sealed is None or current != self.sealed):
            raise ArtworkError("OUTPUT_IDENTITY_CHANGED", "Validated temporary output changed.", "download")
        return current

    def record(self):
        current = self._inspect(sealed=self.sealed is not None)
        return {"name": self.name, "final_name": self.final_name, **current,
                "sealed": self.sealed is not None}

    def write_stream(self, writer, validator=None):
        """Write once, fsync, then prove validation did not change this file."""
        current = self._inspect()
        if self.sealed is not None or current["size"] != 0:
            raise ArtworkError("OUTPUT_IDENTITY_CHANGED", "Temporary output is not a fresh empty allocation.", "download")
        try:
            fd = os.open(self.name, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=self.directory._fd)
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (self.device, self.inode):
                os.close(fd)
                raise ArtworkError("OUTPUT_IDENTITY_CHANGED", "Temporary output changed before writing.", "download")
            with os.fdopen(fd, "wb") as stream:
                written = writer(stream)
                stream.flush()
                os.fsync(stream.fileno())
        except ArtworkError:
            raise
        except OSError:
            raise ArtworkError("OUTPUT_WRITE_FAILED", "Unable to write the registered temporary file.", "download") from None
        self.sealed = self._inspect()
        validation = validator(self.path, written) if validator is not None else None
        self._inspect(sealed=True)
        return {"written": written, "validation": validation}


def _image_metadata(image, declared_p3):
    bands = image.getbands()
    alpha_band = next((band for band in bands if band.lower() == "a"), None)
    alpha_extrema = image.getchannel(alpha_band).getextrema() if alpha_band else None
    transparency = {"alpha_channel": alpha_band is not None,
                    "alpha_extrema": list(alpha_extrema) if alpha_extrema else None,
                    "transparency_metadata": "transparency" in image.info,
                    "has_transparency_data": bool(getattr(image, "has_transparency_data",
                                                          alpha_band or "transparency" in image.info))}
    raw = image.info.get("icc_profile")
    profile = {"present": raw is not None, "bytes": len(raw) if isinstance(raw, bytes) else 0,
               "sha256": sha256(raw).hexdigest() if isinstance(raw, bytes) else None,
               "valid": None, "description": None}
    findings = []
    if raw is not None:
        try:
            from PIL import ImageCms
        except ImportError:
            findings.append("ICC_PARSER_UNAVAILABLE")
        else:
            try:
                if not isinstance(raw, bytes) or not raw:
                    raise ValueError
                parsed = ImageCms.ImageCmsProfile(BytesIO(raw))
                profile["description"] = ImageCms.getProfileDescription(parsed).strip()[:512]
                profile["valid"] = True
            except (OSError, ValueError, TypeError, ImageCms.PyCMSError):
                profile["valid"] = False
                findings.append("ICC_PROFILE_INVALID")
    srgb = image.info.get("srgb") if image.format == "PNG" else None
    values = tuple(declared_p3 or ())
    true_declared = any(value is True for value in values)
    false_declared = any(value is False for value in values)
    invalid = any(type(value) is not bool for value in values)
    description = (profile["description"] or "").lower().replace(" ", "")
    srgb_evidence = srgb is not None or (profile["valid"] is True and "srgb" in description)
    if (true_declared and false_declared) or (true_declared and srgb_evidence):
        declaration_status = "conflict"
        findings.append("COLOR_DECLARATION_CONFLICT")
    elif invalid:
        declaration_status = "invalid"
    else:
        declaration_status = "unverified" if values else "not_declared"
    if invalid:
        findings.append("COLOR_DECLARATION_INVALID")
    if true_declared:
        findings.append("P3_NOT_VERIFIED")
    return {"actual_mode": image.mode, "transparency": transparency,
            "color": {"icc_profile": profile,
                      "png_srgb": {"present": srgb is not None, "rendering_intent": srgb},
                      "declaration": {"hasP3": list(values), "status": declaration_status},
                      "p3_status": "unverified", "conversion_applied": False,
                      "warnings": findings}}


def validate_image(body: bytes, request: ImageRequest, *,
                   content_type: str | None = None, max_pixels: int = MAX_IMAGE_PIXELS,
                   declared_p3=()) -> dict:
    if not body:
        raise ArtworkError("EMPTY_FILE", "The image response is empty.", "validation")
    if len(body) > MAX_IMAGE_BYTES:
        raise ArtworkError("RESPONSE_TOO_LARGE", "Image exceeds the byte limit.", "validation")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(body)) as image:
                width, height = image.size
                actual_format = image.format
                if width * height > max_pixels:
                    raise ArtworkError("IMAGE_TOO_LARGE", "Decoded image exceeds the pixel limit.", "validation")
                if getattr(image, "n_frames", 1) != 1:
                    raise ArtworkError("MEDIA_TYPE_MISMATCH", "Animated images are not static artwork.", "validation")
                image.verify()
            # verify() checks structure; reopening and load() actually decode pixels.
            with Image.open(BytesIO(body)) as image:
                image.load()
                if image.size != (width, height) or image.format != actual_format:
                    raise ArtworkError("ASSET_VALIDATION_FAILED", "Image changed while decoding.", "validation")
                metadata = _image_metadata(image, declared_p3)
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ArtworkError("IMAGE_TOO_LARGE", "Image exceeds the decoder safety limit.", "validation") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise ArtworkError("ASSET_VALIDATION_FAILED", "Response is not a completely decodable image.", "validation") from None
    expected_format = _FORMATS.get(request.format.lower())
    if actual_format != expected_format:
        raise ArtworkError("MEDIA_TYPE_MISMATCH", "Decoded image format differs from the requested format.", "validation")
    media_type = content_type.split(";", 1)[0].strip().lower() if content_type else None
    if media_type and media_type not in _MIME.get(actual_format, set()) | {"application/octet-stream"}:
        raise ArtworkError("MEDIA_TYPE_MISMATCH", "Response Content-Type does not match decoded artwork.", "validation")
    if ((request.width is not None and width != request.width)
            or (request.height is not None and height != request.height)):
        raise ArtworkError("ASSET_DIMENSION_MISMATCH", "Decoded image dimensions differ from the requested rendition.", "validation")
    return {"actual_dimensions": {"width": width, "height": height},
            "actual_format": actual_format, "bytes": len(body), "sha256": sha256(body).hexdigest(),
            "decoded": True, **metadata}


def output_filename(asset: VisualAsset, request: ImageRequest) -> str:
    # Identity derives from the resolved rendition, not the CDN's path basename.
    identity = sha256(f"{request.url}\n{request.width}x{request.height}\n{request.format}".encode("utf-8")).hexdigest()[:16]
    role = re.sub(r"[^a-z0-9_-]", "-", str(asset.role).lower())[:40] or "image"
    extension = "jpg" if request.format == "jpeg" else request.format
    if extension not in _FORMATS:
        raise ArtworkError("MEDIA_TYPE_MISMATCH", "Unsupported image output format.", "rendition")
    return f"{role}-{identity}.{extension}"


class OutputDirectory:
    """POSIX descriptor-relative output; no symlink traversal or file clobbering.

    Requires an operator-owned output directory. Existing artifacts are never
    removed, even when another artifact in the same request fails.
    """

    def __init__(self, path: str | Path, *, create=True):
        self.path = Path(path)
        self.create = create
        self._fd = None
        self._absolute = None
        self._parent = None
        self._component = None
        self.created = False

    def __enter__(self):
        if self._parent is not None:
            if self._parent._fd is None:
                raise RuntimeError("Parent output directory is not open")
            try:
                if self.create:
                    try:
                        os.mkdir(self._component, mode=0o755, dir_fd=self._parent._fd)
                        self.created = True
                        os.fsync(self._parent._fd)
                    except FileExistsError:
                        pass
                self._fd = os.open(self._component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=self._parent._fd)
                self._absolute = self._parent._absolute / self._component
                return self
            except FileNotFoundError:
                if not self.create:
                    raise
                raise ArtworkError("UNSAFE_OUTPUT_PATH", "Output parent changed while opening a child.", "download") from None
            except OSError:
                raise ArtworkError("UNSAFE_OUTPUT_PATH", "Output child is inaccessible or is a symbolic link.", "download") from None
        required = (os.open, os.mkdir, os.stat, os.link, os.unlink)
        if (not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
                or any(function not in os.supports_dir_fd for function in required)):
            raise ArtworkError("OUTPUT_PLATFORM_UNSUPPORTED", "Safe downloads currently require POSIX directory-descriptor support.", "download")
        if ".." in self.path.parts:
            raise ArtworkError("UNSAFE_OUTPUT_PATH", "Output path must not contain parent traversal.", "input")
        absolute = self.path if self.path.is_absolute() else Path.cwd() / self.path
        if not absolute.parts[1:]:
            raise ArtworkError("UNSAFE_OUTPUT_PATH", "Filesystem root is not an output directory.", "input")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(absolute.anchor, flags)
        try:
            for index, component in enumerate(absolute.parts[1:], 1):
                if self.create:
                    try:
                        os.mkdir(component, mode=0o755, dir_fd=fd)
                        if index == len(absolute.parts) - 1:
                            self.created = True
                    except FileExistsError:
                        pass
                child = os.open(component, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            self._fd = fd
            self._absolute = absolute
            return self
        except FileNotFoundError:
            os.close(fd)
            if not self.create:
                raise
            raise ArtworkError("UNSAFE_OUTPUT_PATH", "Output directory disappeared while opening it.", "download") from None
        except OSError:
            os.close(fd)
            raise ArtworkError("UNSAFE_OUTPUT_PATH", "Output directory is inaccessible or contains a symbolic link.", "download") from None

    def __exit__(self, *args):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            self._absolute = None

    def child(self, name, *, create=False):
        """Open one controlled directory relative to this pinned descriptor.

        Child components never enter file or temporary-record name validation.
        Only the two application metadata directories may start with a dot.
        """
        if (not isinstance(name, str) or name in {".", ".."}
                or (name not in {".apple-artwork-state", ".apple-artwork-library"}
                    and (not name or not name[0].isalnum() or len(name.encode("utf-8")) > 240
                         or any(not (char.isalnum() or char in " ._-") for char in name)))):
            raise ArtworkError("UNSAFE_OUTPUT_PATH", "Invalid output directory component.", "download")
        child = OutputDirectory(self.path / name, create=create)
        child._parent, child._component = self, name
        return child

    def _name(self, name):
        if (not isinstance(name, str) or name in {".", ".."}
                or (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", name) is None
                    and _TEMPORARY_NAME.fullmatch(name) is None)):
            raise ArtworkError("UNSAFE_OUTPUT_PATH", "Invalid output filename.", "download")
        if self._fd is None:
            raise RuntimeError("Output directory is not open")

    def ensure_available(self, names):
        for name in names:
            self._name(name)
            try:
                os.stat(name, dir_fd=self._fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                raise ArtworkError("OUTPUT_WRITE_FAILED", "Cannot inspect output destination.", "download") from None
            raise ArtworkError("OUTPUT_CONFLICT", f"Output already exists: {name}", "download")

    def file_identity(self, name):
        self._name(name)
        try:
            info = os.stat(name, dir_fd=self._fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError:
            raise ArtworkError("OUTPUT_READ_FAILED", "Unable to inspect an output file.", "download") from None
        if not stat.S_ISREG(info.st_mode):
            raise ArtworkError("OUTPUT_CONFLICT", "An output path is not an ordinary file.", "download")
        return _identity(info)

    def read_file(self, name, *, max_bytes, check=None):
        """Read only the descriptor-relative regular file whose identity we checked."""
        self._name(name)
        identity = self.file_identity(name)
        if identity is None:
            raise FileNotFoundError(name)
        if identity["size"] > max_bytes:
            raise ArtworkError("OUTPUT_TOO_LARGE", "Existing output exceeds the supported size limit.", "download")
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._fd)
            with os.fdopen(fd, "rb") as stream:
                if _identity(os.fstat(stream.fileno())) != identity:
                    raise ArtworkError("OUTPUT_IDENTITY_CHANGED", "Existing output changed before reading.", "download")
                pieces, received = [], 0
                while True:
                    if check is not None:
                        check()
                    chunk = stream.read(min(1024 * 1024, max_bytes - received + 1))
                    if not chunk:
                        break
                    pieces.append(chunk)
                    received += len(chunk)
                    if received > max_bytes:
                        raise ArtworkError("OUTPUT_TOO_LARGE", "Existing output exceeds the supported size limit.", "download")
                if _identity(os.fstat(stream.fileno())) != identity or self.file_identity(name) != identity:
                    raise ArtworkError("OUTPUT_IDENTITY_CHANGED", "Existing output changed while reading.", "download")
                return b"".join(pieces), identity
        except ArtworkError:
            raise
        except OSError:
            raise ArtworkError("OUTPUT_READ_FAILED", "Unable to read an existing output file.", "download") from None

    def hash_file(self, name, *, max_bytes, check=None):
        self._name(name)
        identity = self.file_identity(name)
        if identity is None:
            raise FileNotFoundError(name)
        if identity["size"] > max_bytes:
            raise ArtworkError("OUTPUT_TOO_LARGE", "Existing output exceeds the supported size limit.", "download")
        digest, received = sha256(), 0
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._fd)
            with os.fdopen(fd, "rb") as stream:
                if _identity(os.fstat(stream.fileno())) != identity:
                    raise ArtworkError("OUTPUT_IDENTITY_CHANGED", "Existing output changed before hashing.", "download")
                while True:
                    if check is not None:
                        check()
                    chunk = stream.read(min(1024 * 1024, max_bytes - received + 1))
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > max_bytes:
                        raise ArtworkError("OUTPUT_TOO_LARGE", "Existing output exceeds the supported size limit.", "download")
                    digest.update(chunk)
                if _identity(os.fstat(stream.fileno())) != identity or self.file_identity(name) != identity:
                    raise ArtworkError("OUTPUT_IDENTITY_CHANGED", "Existing output changed while hashing.", "download")
        except ArtworkError:
            raise
        except OSError:
            raise ArtworkError("OUTPUT_READ_FAILED", "Unable to hash an existing output file.", "download") from None
        return {"bytes": received, "sha256": digest.hexdigest(), "identity": identity}

    def allocate_temp(self, final_name):
        self._name(final_name)
        name = ".apple-artwork-" + uuid4().hex + ".part"
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o644, dir_fd=self._fd)
            try:
                info = os.fstat(fd)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(self._fd)
        except OSError:
            raise ArtworkError("OUTPUT_WRITE_FAILED", "Unable to allocate an owned temporary file.", "download") from None
        return OwnedTemporary(self, name, final_name, info.st_dev, info.st_ino)

    def restore_temp(self, record):
        if (not isinstance(record, dict) or not isinstance(record.get("name"), str)
                or _TEMPORARY_NAME.fullmatch(record["name"]) is None
                or any(type(record.get(key)) is not int or record[key] < 0
                       for key in ("device", "inode", "size", "mtime_ns"))
                or type(record.get("sealed")) is not bool):
            raise ArtworkError("RUN_STATE_INVALID", "Registered temporary identity is invalid.", "download")
        self._name(record.get("final_name", ""))
        temporary = OwnedTemporary(self, record["name"], record["final_name"], record["device"], record["inode"],
                                   {key: record[key] for key in ("device", "inode", "size", "mtime_ns")}
                                   if record["sealed"] else None)
        temporary._inspect(sealed=record["sealed"])
        return temporary

    def publish_temp(self, temporary, *, replace=False, expected_destination=None):
        """Publish only; the coordinator checkpoints before cleaning the temp link."""
        if not isinstance(temporary, OwnedTemporary) or temporary.directory is not self:
            raise ArtworkError("OUTPUT_IDENTITY_CHANGED", "Temporary output belongs to another directory.", "download")
        temporary._inspect(sealed=True)
        committed = False
        try:
            if replace:
                # Replacement is deliberately unavailable for media. The caller
                # must have durably registered the recognized previous manifest.
                if temporary.final_name != "manifest.json" or not isinstance(expected_destination, dict):
                    raise ArtworkError("OUTPUT_CONFLICT", "Only a registered tool manifest may be replaced.", "download")
                previous = self.hash_file("manifest.json", max_bytes=32 * 1024 * 1024)
                if any(previous[key] != expected_destination.get(key) for key in ("bytes", "sha256")):
                    raise ArtworkError("OUTPUT_CONFLICT", "Existing manifest changed outside this task.", "download")
                os.replace(temporary.name, temporary.final_name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
            else:
                os.link(temporary.name, temporary.final_name, src_dir_fd=self._fd, dst_dir_fd=self._fd, follow_symlinks=False)
            committed = True
            os.fsync(self._fd)
        except ArtworkError:
            raise
        except FileExistsError:
            raise OutputCommitError("OUTPUT_CONFLICT", "Output already exists.", path=temporary.final_name,
                                    committed=committed, residue=(temporary.name,)) from None
        except OSError:
            raise OutputCommitError("OUTPUT_WRITE_FAILED", "Unable to publish or synchronize the output file.",
                                    path=temporary.final_name, committed=committed, residue=(temporary.name,)) from None
        return {"path": temporary.final_name, "committed": True,
                "residue": [] if replace else [temporary.name]}

    def cleanup_temp(self, temporary):
        if not isinstance(temporary, OwnedTemporary) or temporary.directory is not self:
            raise ArtworkError("OUTPUT_IDENTITY_CHANGED", "Temporary output belongs to another directory.", "download")
        temporary._inspect(sealed=temporary.sealed is not None)
        try:
            os.unlink(temporary.name, dir_fd=self._fd)
            os.fsync(self._fd)
        except OSError:
            raise OutputCommitError("OUTPUT_CLEANUP_FAILED", "Registered temporary output could not be removed.",
                                    path=temporary.final_name, residue=(temporary.name,)) from None

    def temporary_exists(self, name):
        if not isinstance(name, str) or _TEMPORARY_NAME.fullmatch(name) is None:
            raise ArtworkError("RUN_STATE_INVALID", "Invalid temporary output name.", "download")
        try:
            os.stat(name, dir_fd=self._fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def names(self):
        return sorted(os.listdir(self._fd))

    def write_bytes(self, name: str, body: bytes):
        return self.write_stream(name, lambda stream: stream.write(body))["output"]

    def write_stream(self, name, writer, validator=None):
        """Write and validate one owned temporary file before atomic publication.

        The writer receives a binary stream; the optional validator receives
        its absolute local path and the writer result. Neither gets a final
        destination that could accidentally replace an existing artifact.
        """
        self._name(name)
        self.ensure_available([name])
        temporary = ".apple-artwork-" + uuid4().hex + ".part"
        temporary_identity = None
        committed = False
        problem = None
        residue = []
        written = None
        validated = None
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o644, dir_fd=self._fd)
            temporary_identity = os.fstat(fd)
            with os.fdopen(fd, "wb") as stream:
                written = writer(stream)
                stream.flush()
                os.fsync(stream.fileno())
            current = os.stat(temporary, dir_fd=self._fd, follow_symlinks=False)
            if (not stat.S_ISREG(current.st_mode)
                    or (current.st_dev, current.st_ino) != (temporary_identity.st_dev, temporary_identity.st_ino)):
                raise ArtworkError("OUTPUT_IDENTITY_CHANGED", "Temporary output identity changed.", "download")
            if validator is not None:
                local_path = self._absolute / temporary
                local = os.stat(local_path, follow_symlinks=False)
                if (not stat.S_ISREG(local.st_mode)
                        or (local.st_dev, local.st_ino) != (current.st_dev, current.st_ino)):
                    raise ArtworkError("OUTPUT_IDENTITY_CHANGED", "Temporary validation path changed.", "download")
                validated = validator(local_path, written)
                checked = os.stat(temporary, dir_fd=self._fd, follow_symlinks=False)
                local_after = os.stat(local_path, follow_symlinks=False)
                identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
                if identity(checked) != identity(current) or identity(local_after) != identity(current):
                    raise ArtworkError("OUTPUT_IDENTITY_CHANGED", "Temporary output changed during validation.", "download")
            # Hard-link publication is atomic and fails if the destination exists.
            os.link(temporary, name, src_dir_fd=self._fd, dst_dir_fd=self._fd, follow_symlinks=False)
            committed = True
        except ArtworkError as error:
            problem = error
        except FileExistsError:
            problem = ArtworkError("OUTPUT_CONFLICT", f"Output already exists: {name}", "download")
        except OSError:
            problem = ArtworkError("OUTPUT_WRITE_FAILED", "Unable to commit output file.", "download")
        finally:
            if temporary_identity is not None:
                residue = [temporary]
                try:
                    current = os.stat(temporary, dir_fd=self._fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (temporary_identity.st_dev, temporary_identity.st_ino):
                        raise ArtworkError("OUTPUT_IDENTITY_CHANGED", "Temporary output was replaced; retained for inspection.", "download")
                    os.unlink(temporary, dir_fd=self._fd)
                    residue = []
                except ArtworkError as error:
                    problem = error
                except FileNotFoundError:
                    residue = []
                    problem = ArtworkError("OUTPUT_IDENTITY_CHANGED", "Temporary output disappeared before cleanup.", "download")
                except OSError:
                    problem = ArtworkError("OUTPUT_CLEANUP_FAILED", "Owned temporary output could not be removed.", "download")
        if problem is not None:
            raise OutputCommitError(problem.code, problem.message, path=name,
                                    committed=committed, residue=residue,
                                    category=problem.category, retryable=problem.retryable, context=problem.context) from None
        return {"output": {"path": name, "committed": True, "residue": []},
                "written": written, "validation": validated}


def download_image(asset: VisualAsset, request: ImageRequest, output: OutputDirectory,
                   http: HttpClient, *, declared_p3=None) -> dict:
    filename = output_filename(asset, request)
    output.ensure_available([filename])
    response = http.get(request.url, purpose="image", max_bytes=MAX_IMAGE_BYTES)
    if response.status != 200:
        raise ArtworkError("DOWNLOAD_FAILED", f"Artwork request returned HTTP {response.status}.", "download")
    content_type = next((value for key, value in response.headers.items() if key.lower() == "content-type"), None)
    if declared_p3 is None:
        declared_p3 = tuple(item.value for item in asset.declarations if item.name == "hasP3")
    validation = validate_image(response.body, request, content_type=content_type, declared_p3=declared_p3)
    artifact = {"asset_id": asset.asset_id, "source_identity": asset.source_identity,
            "semantic_path": asset.semantic_path, "url": request.url, "path": filename,
            "declared_dimensions": {"width": asset.width, "height": asset.height},
            "requested_dimensions": {"width": request.width, "height": request.height},
            "requested_format": request.format, "status": "downloaded", "validation": validation}
    try:
        output.write_bytes(filename, response.body)
    except OutputCommitError as error:
        if error.committed:
            error.artifact = {**artifact, "output": error.output_state(), "cleanup_error": error.to_dict()}
        raise
    return artifact


def prepare_image(asset: VisualAsset, request: ImageRequest, temporary: OwnedTemporary,
                  http: HttpClient, *, declared_p3=None, control=None, item_id=None,
                  deadline=None) -> dict:
    """One transfer attempt into a registered file, without publishing it."""
    if control is not None:
        control.check(deadline)
    response = http.get(request.url, purpose="image", max_bytes=MAX_IMAGE_BYTES,
                        max_attempts=1, deadline=deadline)
    if response.status != 200:
        raise ArtworkError("DOWNLOAD_FAILED", f"Artwork request returned HTTP {response.status}.", "download")
    content_type = next((value for key, value in response.headers.items() if key.lower() == "content-type"), None)
    values = declared_p3
    if values is None:
        values = tuple(item.value for item in asset.declarations if item.name == "hasP3")

    def validator(path, written):
        if control is not None:
            control.check(category="validation")
            control.emit({"kind": "stage", "stage": "validate", "item_id": item_id})
        if written != len(response.body):
            raise ArtworkError("OUTPUT_WRITE_FAILED", "Image output write was incomplete.", "download")
        return validate_image(response.body, request, content_type=content_type, declared_p3=values)

    prepared = temporary.write_stream(lambda stream: stream.write(response.body), validator)
    return {"asset_id": asset.asset_id, "source_identity": asset.source_identity,
            "semantic_path": asset.semantic_path, "url": request.url, "path": temporary.final_name,
            "declared_dimensions": {"width": asset.width, "height": asset.height},
            "requested_dimensions": {"width": request.width, "height": request.height},
            "requested_format": request.format, "kind": "image", "status": "downloaded",
            "validation": prepared["validation"]}
