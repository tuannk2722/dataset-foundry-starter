"""Version 1 config parser: TOML, stdlib only, with fail-closed types."""

import math
import re
import tomllib
from pathlib import Path
from typing import Any

from .adapters.discovery import adapter_names
from .models import Limits, Settings, Source

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _boolean(row: dict[str, Any], key: str, default: bool) -> bool:
    value = row.get(key, default)
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a TOML boolean")
    return value


def _string(row: dict[str, Any], key: str, default: str = "") -> str:
    value = row.get(key, default)
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _strings(row: dict[str, Any], key: str) -> tuple[str, ...]:
    value = row.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"{key} must be an array of strings")
    return tuple(value)


def _load_limits(data: dict[str, Any]) -> Limits:
    values = data.get("limits", {})
    if not isinstance(values, dict):
        raise TypeError("limits must be a TOML table")
    try:
        limits = Limits(**values)
    except TypeError as exc:
        raise ValueError(f"invalid limits: {exc}") from exc
    integer_limits = (
        limits.max_asset_mb,
        limits.max_assets_per_source,
        limits.max_zip_compression_ratio,
    )
    if any(not isinstance(value, int) or isinstance(value, bool) for value in integer_limits):
        raise TypeError("asset, file-count, and ZIP limits must be integers")
    delay = limits.min_request_delay_seconds
    if (
        not isinstance(delay, (int, float))
        or isinstance(delay, bool)
        or not math.isfinite(delay)
        or not 0.5 <= delay <= 3600
    ):
        raise ValueError("min_request_delay_seconds must be between 0.5 and 3600")
    if not (0 < limits.max_asset_mb <= 2048 and 0 < limits.max_assets_per_source <= 100000):
        raise ValueError("limits must have positive bounded values")
    if not (1 <= limits.max_zip_compression_ratio <= 10000):
        raise ValueError("max_zip_compression_ratio must be between 1 and 10000")
    return limits


def load_settings(config_path: str | Path) -> Settings:
    path = Path(config_path).resolve()
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if not isinstance(data, dict):
        raise TypeError("configuration root must be a TOML table")
    project = data.get("project", {})
    if not isinstance(project, dict):
        raise TypeError("project must be a TOML table")
    name = _string(project, "name", "unnamed")
    limits = _load_limits(data)
    source_rows = data.get("sources", [])
    if not isinstance(source_rows, list) or any(not isinstance(row, dict) for row in source_rows):
        raise ValueError("sources must be an array of TOML tables")

    sources = []
    seen = set()
    for row in source_rows:
        sid = _string(row, "id")
        adapter = _string(row, "adapter")
        if not ID_RE.fullmatch(sid) or sid in seen:
            raise ValueError(f"Invalid or duplicate source ID: {sid!r}")
        seen.add(sid)
        supported_adapters = adapter_names()
        if adapter not in supported_adapters:
            raise ValueError(f"Unknown adapter {adapter!r} (supported: {list(supported_adapters)})")
        rel = row.get("path")
        if rel is not None and not isinstance(rel, str):
            raise TypeError(f"Source {sid!r} path must be a string")
        include_extensions = tuple(
            extension.lower() for extension in _strings(row, "include_extensions")
        )
        if any(
            not extension.startswith(".") or len(extension) > 12 or not extension[1:].isalnum()
            for extension in include_extensions
        ):
            raise ValueError(f"Source {sid!r} has an invalid include extension")
        source = Source(
            id=sid,
            adapter=adapter,
            enabled=_boolean(row, "enabled", False),
            rights_confirmed=_boolean(row, "rights_confirmed", False),
            license=_string(row, "license", "unknown"),
            rights_note=_string(row, "rights_note"),
            path=(path.parent / rel).resolve() if rel is not None else None,
            urls=_strings(row, "urls"),
            repo_id=_string(row, "repo_id"),
            revision=_string(row, "revision", "main"),
            include_extensions=include_extensions,
            recursive=_boolean(row, "recursive", True),
        )
        if source.enabled and not source.rights_note.strip():
            raise ValueError(f"Source {sid!r} needs rights_note before enabling")
        if (
            source.enabled
            and source.rights_confirmed
            and source.license.strip().lower() in {"", "unknown"}
        ):
            raise ValueError(f"Source {sid!r} needs a known license before collection")
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
