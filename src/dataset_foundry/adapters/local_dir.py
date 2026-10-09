"""Local directory adapter. Symlinks are ignored."""
from collections.abc import Iterator

from ..models import Candidate, Limits, Source
from .base import allowed_extension


def discover_local_dir(source: Source, limits: Limits) -> Iterator[Candidate]:
    assert source.path is not None
    if not source.path.is_dir():
        raise FileNotFoundError(source.path)
    paths = source.path.rglob("*") if source.recursive else source.path.glob("*")
    count = 0
    for path in sorted(paths):
        if path.is_symlink() or not path.is_file():
            continue
        if not allowed_extension(path.name, source.include_extensions):
            continue
        yield Candidate(source.id, "local", str(path), path.name, path=path)
        count += 1
        if count >= limits.max_assets_per_source:
            return
