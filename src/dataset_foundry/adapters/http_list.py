"""Explicit file URL registry (no site crawling or HTML page discovery)."""
from collections.abc import Iterator
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from ..models import Candidate, Limits, Source
from .base import allowed_extension


def discover_http_list(source: Source, limits: Limits) -> Iterator[Candidate]:
    count = 0
    for url in source.urls:
        pathname = unquote(urlsplit(url).path)
        if not allowed_extension(pathname, source.include_extensions):
            continue
        name = PurePosixPath(pathname).name or "asset.bin"
        yield Candidate(source.id, "http", url, name)
        count += 1
        if count >= limits.max_assets_per_source:
            return
