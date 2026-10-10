"""Resolve public catalog URLs without using page names as resource identity."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit

from .errors import ArtworkError
from .models import ResourceKey, Target

_RESOURCE_TYPES = {
    "playlist": "playlists",
    "album": "albums",
    "artist": "artists",
    "song": "songs",
}
_HOSTS = {"music.apple.com", "itunes.apple.com"}


def _invalid(message: str = "Use a public HTTPS Apple Music resource URL.") -> ArtworkError:
    return ArtworkError("INVALID_URL", message, "input")


def _segment(value: str) -> str:
    decoded = value
    # Reject encoded separators and dot segments before any endpoint can be
    # constructed. Double encoding must not turn into a hidden path boundary.
    for _ in range(4):
        if decoded in {".", ".."} or any(c in decoded for c in "/\\"):
            raise _invalid("URL path contains an unsafe segment.")
        if any(ord(c) < 32 or ord(c) == 127 for c in decoded):
            raise _invalid("URL path contains an unsafe segment.")
        next_value = unquote(decoded, encoding="utf-8", errors="strict")
        if next_value == decoded:
            return decoded
        decoded = next_value
    raise _invalid("URL path contains excessive nested escaping.")


def parse_url(url: str) -> Target:
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise _invalid()
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise _invalid()
    if re.search(r"%(?![0-9A-Fa-f]{2})", url):
        raise _invalid("URL contains invalid escaping.")
    try:
        url.encode("utf-8", errors="strict")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            raise _invalid()
        raw_parts = parsed.path.split("/")
        if raw_parts and raw_parts[-1] == "":
            raw_parts = raw_parts[:-1]
        if not raw_parts or raw_parts[0] != "" or any(not x for x in raw_parts[1:]):
            raise _invalid("URL path does not identify one catalog resource.")
        parts = [_segment(item) for item in raw_parts[1:]]
        if len(parts) not in (3, 4):
            raise _invalid("URL path does not identify one catalog resource.")
        storefront, resource_type = parts[:2]
        if re.fullmatch(r"[a-z]{2}", storefront) is None:
            raise _invalid("URL must include a two-letter storefront.")
        if resource_type not in _RESOURCE_TYPES:
            raise ArtworkError(
                "UNSUPPORTED_RESOURCE", "Supported resources are playlist, album, artist and song.", "input"
            )
        resource_id = parts[-1]
        id_pattern = r"pl\.[A-Za-z0-9][A-Za-z0-9._-]*" if resource_type == "playlist" else r"[0-9]+"
        if len(resource_id) > 512 or re.fullmatch(id_pattern, resource_id) is None:
            raise _invalid("URL contains an invalid catalog resource ID.")
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=64)
        for key in ("i", "l"):
            if key in query and (len(query[key]) != 1 or not query[key][0]):
                raise _invalid("URL contains an ambiguous catalog query parameter.")
        locale = query.get("l", [None])[0]
        if locale is not None and (
            len(locale) > 64 or re.fullmatch(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*", locale) is None
        ):
            raise _invalid("URL locale is invalid.")
        song_id = query.get("i", [None])[0]
        album_id = None
        if song_id is not None:
            if resource_type != "album" or re.fullmatch(r"[0-9]{1,64}", song_id) is None:
                raise _invalid("A song query must identify a song on an album URL.")
            album_id, resource_id = resource_id, song_id
            api_type = "songs"
        else:
            api_type = _RESOURCE_TYPES[resource_type]
        used_query = []
        if song_id is not None:
            used_query.append(("i", song_id))
        if locale is not None:
            used_query.append(("l", locale))
        canonical_url = urlunsplit(
            ("https", parsed.hostname, "/".join(raw_parts), urlencode(used_query), "")
        )
        return Target(ResourceKey(storefront, api_type, resource_id), canonical_url, locale, album_id)
    except ArtworkError:
        raise
    except (ValueError, UnicodeError) as error:
        raise _invalid() from error
