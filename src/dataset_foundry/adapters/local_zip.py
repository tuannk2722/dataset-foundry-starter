"""Local ZIP adapter: enumerate safe bounded entries without extracting all content."""

from collections.abc import Iterator
from pathlib import PurePosixPath
from zipfile import ZipFile

from ..models import Candidate, Limits, Source
from .base import allowed_extension


def discover_local_zip(source: Source, limits: Limits) -> Iterator[Candidate]:
    assert source.path is not None
    count = 0
    with ZipFile(source.path) as zf:
        for entry in zf.infolist():
            if entry.is_dir():
                continue
            count += 1
            if count > limits.max_assets_per_source:
                return
            if not allowed_extension(entry.filename, source.include_extensions):
                continue
            normalized = entry.filename.replace("\\", "/")
            path = PurePosixPath(normalized)
            reason = None
            if (
                not path.parts
                or normalized.startswith("/")
                or ".." in path.parts
                or ":" in path.parts[0]
            ):
                reason = "unsafe archive member path"
            elif entry.flag_bits & 0x1:
                reason = "encrypted archive member disallowed"
            elif entry.file_size > limits.max_bytes:
                reason = "archive member exceeds configured size limit"
            elif entry.file_size > max(1, entry.compress_size) * limits.max_zip_compression_ratio:
                reason = "archive member exceeds configured compression ratio"
            elif ((entry.external_attr >> 16) & 0o170000) == 0o120000:
                reason = "archive symlink disallowed"
            yield Candidate(
                source.id,
                "zip_member",
                f"{source.path}!{entry.filename}",
                path.name or "archive-member.bin",
                path=source.path,
                member=entry.filename,
                meta={"skip_reason": reason} if reason else {},
            )
