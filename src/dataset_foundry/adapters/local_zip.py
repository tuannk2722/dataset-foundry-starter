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
            if entry.is_dir() or not allowed_extension(entry.filename, source.include_extensions):
                continue
            normalized = entry.filename.replace("\\", "/")
            path = PurePosixPath(normalized)
            if not path.parts or normalized.startswith("/") or ".." in path.parts or ":" in path.parts[0]:
                continue
            if entry.flag_bits & 0x1:  # encrypted member
                continue
            if entry.file_size > limits.max_bytes:
                continue
            if entry.file_size > max(1, entry.compress_size) * limits.max_zip_compression_ratio:
                continue
            if ((entry.external_attr >> 16) & 0o170000) == 0o120000:  # Unix symlink
                continue
            yield Candidate(
                source.id, "zip_member", f"{source.path}!{entry.filename}",
                path.name, path=source.path, member=entry.filename,
            )
            count += 1
            if count >= limits.max_assets_per_source:
                return
