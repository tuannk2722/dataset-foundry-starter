"""Small registry keeps source implementations isolated for parallel AI agents."""
from collections.abc import Iterator

from ..models import Candidate, Limits, Source
from .base import DiscoveryAdapter
from .hf_dataset import discover_hf_dataset
from .http_list import discover_http_list
from .local_dir import discover_local_dir
from .local_zip import discover_local_zip

ADAPTERS: dict[str, DiscoveryAdapter] = {
    "local_dir": discover_local_dir,
    "local_zip": discover_local_zip,
    "http_list": discover_http_list,
    "hf_dataset": discover_hf_dataset,
}


def discover(source: Source, limits: Limits) -> Iterator[Candidate]:
    try:
        adapter = ADAPTERS[source.adapter]
    except KeyError as exc:
        raise ValueError(f"Unregistered adapter: {source.adapter}") from exc
    yield from adapter(source, limits)
