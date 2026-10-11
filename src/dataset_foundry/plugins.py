"""Load trusted third-party adapters and processors from package entry points."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
from threading import RLock
from typing import Any

from .adapters.discovery import (
    ADAPTER_NAME_RE,
    ADAPTERS,
    register_adapter,
    unregister_adapter,
)
from .processors import (
    PROCESSOR_NAME_RE,
    PROCESSORS,
    register_processor,
    unregister_processor,
)

ADAPTER_ENTRY_POINT_GROUP = "dataset_foundry.adapters"
PROCESSOR_ENTRY_POINT_GROUP = "dataset_foundry.processors"
ENTRY_POINT_GROUPS = (ADAPTER_ENTRY_POINT_GROUP, PROCESSOR_ENTRY_POINT_GROUP)


@dataclass(frozen=True)
class _LoadedEntryPoint:
    identity: tuple[str, str]
    plugin: Any


_LOADED_ENTRY_POINTS: dict[tuple[str, str], _LoadedEntryPoint] = {}
_LOAD_LOCK = RLock()


def _entry_point_identity(entry_point: metadata.EntryPoint) -> tuple[str, str]:
    distribution = getattr(entry_point, "dist", None)
    distribution_name = getattr(distribution, "name", "")
    return entry_point.value, str(distribution_name or "")


def _registry(group: str) -> tuple[dict, Any, Any, Any]:
    if group == ADAPTER_ENTRY_POINT_GROUP:
        return ADAPTERS, ADAPTER_NAME_RE, register_adapter, unregister_adapter
    if group == PROCESSOR_ENTRY_POINT_GROUP:
        return PROCESSORS, PROCESSOR_NAME_RE, register_processor, unregister_processor
    raise ValueError(f"Unsupported entry-point group: {group}")


def _discover_entry_points() -> list[tuple[str, metadata.EntryPoint]]:
    group_order = {group: index for index, group in enumerate(ENTRY_POINT_GROUPS)}
    discovered = [
        (group, entry_point)
        for group in ENTRY_POINT_GROUPS
        for entry_point in metadata.entry_points(group=group)
    ]
    return sorted(
        discovered,
        key=lambda item: (
            group_order[item[0]],
            item[1].name,
            item[1].value,
            _entry_point_identity(item[1])[1],
        ),
    )


def load_entry_point_plugins() -> dict[str, tuple[str, ...]]:
    """Load and register installed Dataset Foundry plugins once per process.

    All names and collisions are checked before plugin code is imported. All loaded
    objects are then checked before any registry is mutated, so a bad plugin cannot
    leave a partially registered set behind.
    """

    with _LOAD_LOCK:
        discovered = _discover_entry_points()
        names = {group: [] for group in ENTRY_POINT_GROUPS}
        seen: set[tuple[str, str]] = set()
        pending: list[tuple[str, metadata.EntryPoint, _LoadedEntryPoint | None]] = []

        for group, entry_point in discovered:
            key = (group, entry_point.name)
            registry, name_pattern, _, _ = _registry(group)
            if not isinstance(entry_point.name, str) or not name_pattern.fullmatch(
                entry_point.name
            ):
                raise ValueError(
                    f"Invalid plugin entry-point name {entry_point.name!r} in group {group!r}"
                )
            if key in seen:
                raise ValueError(
                    f"Duplicate plugin entry point {entry_point.name!r} in group {group!r}"
                )
            seen.add(key)
            names[group].append(entry_point.name)

            loaded = _LOADED_ENTRY_POINTS.get(key)
            identity = _entry_point_identity(entry_point)
            if loaded is not None:
                if loaded.identity != identity:
                    raise ValueError(
                        f"Plugin entry point {entry_point.name!r} in group {group!r} changed "
                        "after it was loaded"
                    )
                registered = registry.get(entry_point.name)
                if registered is not None:
                    if registered is not loaded.plugin:
                        raise ValueError(
                            f"Plugin entry point {entry_point.name!r} collides with an "
                            f"existing registration in group {group!r}"
                        )
                    continue
            elif entry_point.name in registry:
                raise ValueError(
                    f"Plugin entry point {entry_point.name!r} collides with an existing "
                    f"registration in group {group!r}"
                )
            pending.append((group, entry_point, loaded))

        prepared: list[tuple[str, metadata.EntryPoint, Any]] = []
        for group, entry_point, loaded in pending:
            if loaded is None:
                try:
                    plugin = entry_point.load()
                except Exception as exc:
                    raise RuntimeError(
                        f"Failed to load plugin entry point {entry_point.name!r} "
                        f"from group {group!r}"
                    ) from exc
            else:
                plugin = loaded.plugin
            if not callable(plugin):
                raise TypeError(
                    f"Plugin entry point {entry_point.name!r} from group {group!r} "
                    "must resolve to a callable"
                )
            prepared.append((group, entry_point, plugin))

        registered: list[tuple[str, str]] = []
        try:
            for group, entry_point, plugin in prepared:
                _, _, register, _ = _registry(group)
                register(entry_point.name, plugin)
                registered.append((group, entry_point.name))
        except Exception:
            for group, name in reversed(registered):
                _, _, _, unregister = _registry(group)
                unregister(name)
            raise

        for group, entry_point, plugin in prepared:
            _LOADED_ENTRY_POINTS[(group, entry_point.name)] = _LoadedEntryPoint(
                identity=_entry_point_identity(entry_point),
                plugin=plugin,
            )
        return {group: tuple(names[group]) for group in ENTRY_POINT_GROUPS}
