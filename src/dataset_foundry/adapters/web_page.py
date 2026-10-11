"""Explicit approved web pages collected as HTML, optionally rendered with JavaScript."""

from collections.abc import Iterator
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from ..models import Candidate, Limits, Source


def discover_web_pages(source: Source, limits: Limits) -> Iterator[Candidate]:
    for count, url in enumerate(source.urls):
        if count >= limits.max_assets_per_source:
            return
        pathname = unquote(urlsplit(url).path)
        stem = PurePosixPath(pathname).name or "index"
        name = stem if PurePosixPath(stem).suffix.lower() in {".html", ".htm"} else f"{stem}.html"
        yield Candidate(
            source_id=source.id,
            kind="web_page",
            origin=url,
            name=name,
            meta={"render_engine": source.render_engine},
        )
