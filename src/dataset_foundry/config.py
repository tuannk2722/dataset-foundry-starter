"""Version 1 config parser: TOML, stdlib only."""
import re
import tomllib
from pathlib import Path

from .models import Limits, Settings, Source

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ADAPTERS = {"local_dir", "local_zip", "http_list", "hf_dataset"}


def load_settings(config_path: str | Path) -> Settings:
    path = Path(config_path).resolve()
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    project = data.get("project", {})
    name = project.get("name", "unnamed")
    values = data.get("limits", {})
    limits = Limits(**values)
    if not (0 < limits.max_asset_mb <= 2048 and 0 < limits.max_assets_per_source <= 100000):
        raise ValueError("limits must have positive bounded values")
    if not (1 <= limits.max_zip_compression_ratio <= 10000):
        raise ValueError("max_zip_compression_ratio must be between 1 and 10000")
    if not (limits.min_request_delay_seconds >= 0.5):
        raise ValueError("min_request_delay_seconds must be at least 0.5")
    sources = []
    seen = set()
    for row in data.get("sources", []):
        sid, adapter = row["id"], row["adapter"]
        if not ID_RE.fullmatch(sid) or sid in seen:
            raise ValueError(f"Invalid or duplicate source ID: {sid!r}")
        seen.add(sid)
        if adapter not in ADAPTERS:
            raise ValueError(f"Unknown adapter {adapter!r} (supported: {sorted(ADAPTERS)})")
        rel = row.get("path")
        source = Source(
            id=sid,
            adapter=adapter,
            enabled=bool(row.get("enabled", False)),
            rights_confirmed=bool(row.get("rights_confirmed", False)),
            license=str(row.get("license", "unknown")),
            rights_note=str(row.get("rights_note", "")),
            path=(path.parent / rel).resolve() if rel is not None else None,
            urls=tuple(row.get("urls", [])),
            repo_id=str(row.get("repo_id", "")),
            revision=str(row.get("revision", "main")),
            include_extensions=tuple(x.lower() for x in row.get("include_extensions", [])),
            recursive=bool(row.get("recursive", True)),
        )
        if source.enabled and not source.rights_note:
            raise ValueError(f"Source {sid!r} needs rights_note before enabling")
        if source.adapter in {"local_dir", "local_zip"} and source.path is None:
            raise ValueError(f"Source {sid!r} needs path")
        if source.adapter == "http_list" and not source.urls:
            raise ValueError(f"Source {sid!r} needs urls")
        if source.adapter == "hf_dataset" and not source.repo_id:
            raise ValueError(f"Source {sid!r} needs repo_id")
        sources.append(source)
    if not sources:
        raise ValueError("No sources configured")
    return Settings(name=name, limits=limits, sources=tuple(sources), config_path=path)
