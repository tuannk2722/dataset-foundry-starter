"""Adapter contract: discover bounded, unreviewed candidates."""
from collections.abc import Iterator
from typing import Protocol

from ..models import Candidate, Limits, Source


class DiscoveryAdapter(Protocol):
    def __call__(self, source: Source, limits: Limits) -> Iterator[Candidate]: ...


def allowed_extension(name: str, extensions: tuple[str, ...]) -> bool:
    from pathlib import Path
    return not extensions or Path(name).suffix.lower() in extensions
