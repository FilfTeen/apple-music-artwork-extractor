"""Identify CDN sources separately from the URLs used to request renditions."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit, urlunsplit

from .errors import ArtworkError
from .models import ImageOptions, ImageRequest, VisualAsset, positive_integer

IMAGE_FORMATS = frozenset({"jpg", "jpeg", "png", "webp", "avif", "heic"})
_IGNORED_SOURCE_QUERY = frozenset({"l", "utm_source", "utm_medium", "utm_campaign"})


def image_host(hostname: str | None) -> bool:
    return bool(hostname and (hostname == "mzstatic.com" or hostname.endswith(".mzstatic.com")))


def source_identity(url: str, kind: str) -> str:
    """A grouping hint, never an assertion that this URL is downloadable."""
    if kind != "image":
        # HLS parameters can select or authorize a stream. Preserve the complete
        # address rather than guessing which parameters are mere presentation.
        return url
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    if not image_host(parsed.hostname) or not parsed.path.startswith("/image/thumb/"):
        return url
    parent, _, rendition = parsed.path.rpartition("/")
    hierarchy = parent.removeprefix("/image/thumb/")
    if not hierarchy or parent == "/image/thumb":
        return url
    if not (
        "{w}" in rendition
        or "{h}" in rendition
        or re.match(r"^\d+x\d+", rendition)
    ):
        return url
    # Preserve opaque query bytes; omit only the explicitly listed parameter
    # names from this grouping key.
    query = "&".join(
        part
        for part in parsed.query.split("&")
        if part and unquote(part.partition("=")[0]) not in _IGNORED_SOURCE_QUERY
    )
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parent, query, ""))


def _error(code: str, message: str) -> ArtworkError:
    return ArtworkError(code, message, "rendition")


def resolve_image(asset: VisualAsset, options: ImageOptions | None = None) -> ImageRequest:
    options = options or ImageOptions()
    if asset.kind != "image":
        raise _error("UNSUPPORTED_ASSET_KIND", "Only image assets can be resolved for download.")
    if not positive_integer(asset.width) or not positive_integer(asset.height):
        raise _error("MISSING_DIMENSIONS", "The image has no valid declared width and height.")
    assert asset.width is not None and asset.height is not None
    for value in (options.width, options.height):
        if value is not None and not positive_integer(value):
            raise _error("INVALID_DIMENSIONS", "Requested dimensions must be positive integers.")
    width, height = options.width, options.height
    if width is None and height is None:
        width, height = asset.width, asset.height
    elif width is None:
        assert height is not None
        width = max(1, asset.width * height // asset.height)
    elif height is None:
        height = max(1, asset.height * width // asset.width)
    assert width is not None and height is not None
    if width > asset.width or height > asset.height:
        raise _error("DIMENSIONS_EXCEED_DECLARED", "Requested dimensions exceed the API declaration.")
    if options.format not in IMAGE_FORMATS:
        raise _error("INVALID_IMAGE_FORMAT", "Requested image format is not supported.")
    if options.crop != "bb":
        raise _error("INVALID_CROP_MODE", "Only the observed bb placeholder policy is supported.")
    try:
        parsed = urlsplit(asset.url)
        if (
            parsed.scheme != "https"
            or not image_host(parsed.hostname)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            raise _error("UNSAFE_ASSET_URL", "The image URL must use HTTPS on the Apple image CDN.")
    except ValueError as error:
        raise _error("UNSAFE_ASSET_URL", "The image URL is malformed.") from error
    placeholders = re.findall(r"\{([^{}]*)\}", asset.url)
    if set(placeholders) - {"w", "h", "c", "f"}:
        raise _error("IMAGE_TEMPLATE_INVALID", "The image template has an unknown placeholder.")
    if re.search(r"[{}]", re.sub(r"\{[^{}]*\}", "", asset.url)):
        raise _error("IMAGE_TEMPLATE_INVALID", "The image template contains unmatched braces.")
    if ("w" in placeholders) != ("h" in placeholders):
        raise _error("IMAGE_TEMPLATE_INVALID", "A dimension template must expose both width and height.")
    if "w" not in placeholders and (width, height) != (asset.width, asset.height):
        raise _error("IMAGE_TEMPLATE_INVALID", "This concrete image URL cannot request another size.")
    substitutions = {"w": str(width), "h": str(height), "c": options.crop, "f": options.format}
    resolved = re.sub(r"\{([^{}]*)\}", lambda match: substitutions[match.group(1)], asset.url)
    # Fixed crop/overlay components such as sr, SC.DN01 and SHT.AMTPPS01 are
    # authoritative parts of the supplied rendition, not alternate spellings of bb.
    actual_format = urlsplit(resolved).path.rsplit(".", 1)[-1].lower()
    if actual_format not in IMAGE_FORMATS:
        raise _error("IMAGE_TEMPLATE_INVALID", "The image template has no supported output format.")
    return ImageRequest(resolved, width, height, actual_format, asset.source_identity, asset.asset_id)
