"""Shared data structures; adapters should not depend on pipeline internals."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Source:
    id: str
    adapter: str
    enabled: bool
    rights_confirmed: bool
    license: str
    rights_note: str
    path: Path | None = None
    urls: tuple[str, ...] = ()
    repo_id: str = ""
    revision: str = "main"
    include_extensions: tuple[str, ...] = ()
    recursive: bool = True
    allowed_mime_types: tuple[str, ...] = ()
    render_engine: str = "direct"


@dataclass(frozen=True)
class Limits:
    max_asset_mb: int = 100
    max_source_mb: int = 1024
    max_assets_per_source: int = 100
    max_zip_compression_ratio: int = 100
    min_request_delay_seconds: float = 1.5
    max_workers: int = 4
    retry_attempts: int = 3
    retry_backoff_seconds: float = 1.0
    mime_validation: str = "warn"

    @property
    def max_bytes(self) -> int:
        return self.max_asset_mb * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    name: str
    limits: Limits
    sources: tuple[Source, ...]
    config_path: Path


@dataclass(frozen=True)
class Candidate:
    source_id: str
    kind: str  # local | http | web_page | zip_member | huggingface
    origin: str
    name: str
    path: Path | None = None
    member: str = ""
    repo_id: str = ""
    revision: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
