"""Small registry keeps source implementations isolated for parallel AI agents."""

import re
from collections.abc import Iterator

from ..models import Candidate, Limits, Source
from .base import DiscoveryAdapter
from .hf_dataset import discover_hf_dataset
from .http_list import discover_http_list
from .local_dir import discover_local_dir
from .local_zip import discover_local_zip
from .web_page import discover_web_pages

ADAPTERS: dict[str, DiscoveryAdapter] = {
    "local_dir": discover_local_dir,
    "local_zip": discover_local_zip,
    "http_list": discover_http_list,
    "hf_dataset": discover_hf_dataset,
    "web_page": discover_web_pages,
}
ADAPTER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def register_adapter(name: str, adapter: DiscoveryAdapter, *, replace: bool = False) -> None:
    """Register a discovery adapter for config parsing and collection."""
    if not ADAPTER_NAME_RE.fullmatch(name):
        raise ValueError(f"Invalid adapter name: {name!r}")
    if name in ADAPTERS and not replace:
        raise ValueError(f"Adapter already registered: {name}")
    if not callable(adapter):
        raise TypeError("adapter must be callable")
    ADAPTERS[name] = adapter


def unregister_adapter(name: str) -> None:
    """Remove a dynamically registered adapter."""
    try:
        del ADAPTERS[name]
    except KeyError as exc:
        raise ValueError(f"Adapter is not registered: {name}") from exc


def adapter_names() -> tuple[str, ...]:
    return tuple(sorted(ADAPTERS))


def discover(source: Source, limits: Limits) -> Iterator[Candidate]:
    try:
        adapter = ADAPTERS[source.adapter]
    except KeyError as exc:
        raise ValueError(f"Unregistered adapter: {source.adapter}") from exc
    yield from adapter(source, limits)
