"""Small, dependency-free helpers for conservative content-type validation."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import BinaryIO

DEFAULT_PREFIX_BYTES = 8192
MAX_PREFIX_BYTES = 64 * 1024

_ALIASES = {
    "application/x-gzip": "application/gzip",
    "application/x-zip-compressed": "application/zip",
    "audio/x-wav": "audio/wav",
    "image/jpg": "image/jpeg",
    "text/xml": "application/xml",
}


def read_bounded_prefix(stream: BinaryIO, max_bytes: int = DEFAULT_PREFIX_BYTES) -> bytes:
    """Read a small prefix without allowing callers to request unbounded memory."""
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or not 1 <= max_bytes <= MAX_PREFIX_BYTES
    ):
        raise ValueError(f"max_bytes must be between 1 and {MAX_PREFIX_BYTES}")
    prefix = stream.read(max_bytes)
    if not isinstance(prefix, bytes):
        raise TypeError("binary stream read() must return bytes")
    # Defensive for non-conforming streams that ignore the requested read size.
    return prefix[:max_bytes]


def sniff_mime(prefix: bytes | bytearray | memoryview) -> str | None:
    """Return a MIME type for recognized signatures, otherwise ``None``.

    Only the bounded leading portion is inspected. The result is evidence from a
    signature, not a claim that the complete file is well-formed.
    """
    if not isinstance(prefix, (bytes, bytearray, memoryview)):
        raise TypeError("prefix must be bytes-like")
    data = bytes(prefix[:MAX_PREFIX_BYTES])

    signatures = (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"BM", "image/bmp"),
        (b"II*\x00", "image/tiff"),
        (b"MM\x00*", "image/tiff"),
        (b"\x00\x00\x01\x00", "image/x-icon"),
        (b"%PDF-", "application/pdf"),
        (b"PK\x03\x04", "application/zip"),
        (b"PK\x05\x06", "application/zip"),
        (b"PK\x07\x08", "application/zip"),
        (b"\x1f\x8b", "application/gzip"),
        (b"BZh", "application/x-bzip2"),
        (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
        (b"Rar!\x1a\x07", "application/vnd.rar"),
        (b"fLaC", "audio/flac"),
        (b"OggS", "application/ogg"),
        (b"ID3", "audio/mpeg"),
    )
    for signature, mime in signatures:
        if data.startswith(signature):
            return mime

    if len(data) >= 12 and data.startswith(b"RIFF"):
        form = data[8:12]
        if form == b"WEBP":
            return "image/webp"
        if form == b"WAVE":
            return "audio/wav"
        if form == b"AVI ":
            return "video/x-msvideo"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4"

    stripped = data.lstrip(b"\xef\xbb\xbf\x00\x09\x0a\x0c\x0d\x20")
    lowered = stripped[:32].lower()
    if lowered.startswith(b"<?xml"):
        return "application/xml"
    if lowered.startswith((b"<!doctype html", b"<html")):
        return "text/html"
    if stripped.startswith((b"{", b"[")):
        try:
            json.loads(stripped.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        else:
            return "application/json"
    if data and b"\x00" not in data:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            if all(character.isprintable() or character in "\r\n\t" for character in text):
                return "text/plain"
    return None


def normalize_mime(mime: str) -> str:
    """Normalize case, parameters, and a small set of common aliases."""
    if not isinstance(mime, str):
        raise TypeError("MIME type must be a string")
    normalized = mime.partition(";")[0].strip().lower()
    if normalized.count("/") != 1 or any(character.isspace() for character in normalized):
        raise ValueError(f"invalid MIME type: {mime!r}")
    return _ALIASES.get(normalized, normalized)


def mime_types_compatible(expected: str, detected: str) -> bool:
    """Check whether declared/guessed and signature MIME types are compatible."""
    expected_normalized = normalize_mime(expected)
    detected_normalized = normalize_mime(detected)
    if expected_normalized == "application/octet-stream":
        return True
    if detected_normalized == "text/plain" and expected_normalized.startswith("text/"):
        return True
    if expected_normalized.endswith("/*"):
        return detected_normalized.startswith(expected_normalized[:-1])
    return expected_normalized == detected_normalized


def mime_allowed(mime: str | None, allowed: Iterable[str]) -> bool:
    """Apply an allow-list supporting exact MIME types and ``type/*`` wildcards.

    An empty allow-list means unrestricted. If restrictions exist, unknown
    signatures fail closed.
    """
    normalized_allowed = tuple(normalize_mime(item) for item in allowed)
    if not normalized_allowed:
        return True
    if mime is None:
        return False
    normalized = normalize_mime(mime)
    return any(
        item == "*/*"
        or item == normalized
        or (item.endswith("/*") and normalized.startswith(item[:-1]))
        for item in normalized_allowed
    )
