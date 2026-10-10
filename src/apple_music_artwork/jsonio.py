"""Strict JSON input and deterministic, noncanonical JSON output."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .errors import ArtworkError

MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_JSON_DEPTH = 128


def _invalid_json(message: str) -> ArtworkError:
    return ArtworkError("INVALID_JSON", message, "input")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid_json("JSON contains a duplicate object key.")
        result[key] = value
    return result


def _constant(_: str) -> Any:
    raise _invalid_json("JSON must not contain non-finite numbers.")


def _float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise _invalid_json("JSON must not contain non-finite numbers.")
    return result


def _validate_utf8_strings(value: Any) -> None:
    # JSON escapes can introduce lone surrogates even when the input bytes are
    # valid UTF-8. Check decoded strings before they reach identifiers or output.
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            item.encode("utf-8", errors="strict")
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _check_depth(text: str) -> None:
    # Decoder recursion limits vary by interpreter. Bound nesting before calling
    # the decoder, without treating braces inside a JSON string as containers.
    depth = 0
    quoted = False
    escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise _invalid_json("JSON nesting exceeds the supported depth limit.")
        elif character in "]}":
            depth -= 1


def loads_json(text: str | bytes) -> Any:
    """Accept normal JSON whitespace/key order, but no duplicate keys or NaN."""
    if not isinstance(text, (str, bytes)):
        raise _invalid_json("JSON input must be text or UTF-8 bytes.")
    try:
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="strict")
        _check_depth(text)
        result = json.loads(text, object_pairs_hook=_object, parse_constant=_constant, parse_float=_float)
        _validate_utf8_strings(result)
        return result
    except ArtworkError:
        raise
    except (UnicodeError, ValueError, RecursionError) as error:
        raise _invalid_json("Input is not valid UTF-8 JSON, or exceeds the parser depth limit.") from error


def load_json(path: str | Path, max_bytes: int = MAX_JSON_BYTES) -> Any:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise _invalid_json("JSON size limit must be a positive integer.")
    try:
        with Path(path).open("rb") as stream:
            body = stream.read(max_bytes + 1)
    except OSError as error:
        raise ArtworkError("INPUT_READ_FAILED", "Unable to read the JSON input file.", "input") from error
    if len(body) > max_bytes:
        raise ArtworkError("INPUT_TOO_LARGE", "JSON input exceeds the configured size limit.", "input")
    return loads_json(body)


def dumps_json(value: Any) -> str:
    """Produce UTF-8-compatible text with a terminal LF; key order is not a gate."""
    try:
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        rendered.encode("utf-8", errors="strict")
        return rendered
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ArtworkError("OUTPUT_JSON_INVALID", "Result cannot be represented as JSON.", "internal") from error
