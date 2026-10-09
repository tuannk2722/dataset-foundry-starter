"""Crash-resumable collection orchestrator, sequential and bounded by design."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import requests

from .adapters.discovery import discover
from .downloader import AssetTooLarge, fetch
from .manifest import read_json, read_jsonl, write_json, write_jsonl
from .models import Candidate, Settings
from .policy import SkipAsset, WebPolicy

RUN_STATE_VERSION = "dataset-foundry-run-v1"
TERMINAL_SOURCE_STATUSES = {"completed", "skipped"}
CHECKPOINT_STATUSES = {"downloaded", "duplicate", "skipped", "error"}
ASSET_ID_RE = re.compile(r"^[0-9a-f]{20}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _candidate_id(candidate: Candidate) -> str:
    fingerprint = f"{candidate.source_id}|{candidate.origin}|{candidate.member}"
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:20]


def _name_ext(name: str) -> str:
    # Only the extension survives. Never trust original filenames as output paths.
    ext = Path(name).suffix.lower()
    if len(ext) > 12 or not ext or not ext[1:].isalnum():
        return ".bin"
    return ext


def _source_rows(settings: Settings) -> list[dict]:
    return [
        {
            "id": source.id,
            "adapter": source.adapter,
            "enabled": source.enabled,
            "rights_confirmed": source.rights_confirmed,
            "license": source.license,
            "rights_note": source.rights_note,
        }
        for source in settings.sources
    ]


def _settings_fingerprint(settings: Settings) -> str:
    sources = []
    for source in settings.sources:
        row = asdict(source)
        row["path"] = str(source.path) if source.path is not None else None
        sources.append(row)
    payload = {
        "name": settings.name,
        "limits": asdict(settings.limits),
        "sources": sources,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _prepare_run(settings: Settings, run_dir: Path, resume: bool) -> dict:
    state_path = run_dir / "run_state.json"
    fingerprint = _settings_fingerprint(settings)
    if resume:
        state = read_json(state_path)
        if not isinstance(state, dict) or state.get("schema_version") != RUN_STATE_VERSION:
            raise ValueError("run_state.json has an unsupported or invalid schema")
        if state.get("config_fingerprint") != fingerprint:
            raise ValueError("resume config does not match the run's original configuration")
    else:
        if run_dir.exists() and any(run_dir.iterdir()):
            raise FileExistsError(
                f"run already exists: {run_dir}. Choose another run directory or use --resume."
            )
        run_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "schema_version": RUN_STATE_VERSION,
            "config_fingerprint": fingerprint,
            "status": "collecting",
            "created_at": _utc_now(),
        }
        write_json(state_path, state)
        write_jsonl(run_dir / "manifest.jsonl", [])
        write_jsonl(run_dir / "events.jsonl", [])
    write_json(run_dir / "sources.json", _source_rows(settings))
    return state


def _hash_file(path: Path, max_bytes: int | None = None) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise AssetTooLarge(f"asset exceeds maximum {max_bytes} bytes")
            digest.update(chunk)
    return size, digest.hexdigest()


def _checkpoint_path(run_dir: Path, asset_id: str) -> Path:
    return run_dir / "checkpoints" / f"{asset_id}.json"


def _load_checkpoints(run_dir: Path) -> list[tuple[int, dict]]:
    checkpoint_dir = run_dir / "checkpoints"
    if not checkpoint_dir.exists():
        return []
    loaded: list[tuple[int, dict]] = []
    seen_sequences: set[int] = set()
    for path in sorted(checkpoint_dir.glob("*.json")):
        payload = read_json(path)
        if not isinstance(payload, dict):
            raise TypeError(f"invalid checkpoint: {path}")
        sequence = payload.get("sequence")
        record = payload.get("record")
        if not isinstance(sequence, int) or sequence < 0 or not isinstance(record, dict):
            raise ValueError(f"invalid checkpoint: {path}")
        if record.get("asset_id") != path.stem or sequence in seen_sequences:
            raise ValueError(f"inconsistent checkpoint: {path}")
        seen_sequences.add(sequence)
        loaded.append((sequence, record))
    return sorted(loaded, key=lambda item: item[0])


def _verify_checkpoint_record(run_dir: Path, record: dict) -> None:
    status = record.get("status")
    asset_id = record.get("asset_id")
    if not isinstance(asset_id, str) or not ASSET_ID_RE.fullmatch(asset_id):
        raise ValueError(f"checkpoint has an invalid asset ID: {asset_id!r}")
    if status not in CHECKPOINT_STATUSES:
        raise ValueError(f"checkpoint {asset_id} has an invalid status: {status!r}")
    if (
        record.get("review_status") != "unreviewed"
        or record.get("label") is not None
        or record.get("split") is not None
    ):
        raise ValueError(f"checkpoint {asset_id} contains promoted review data")
    if status == "downloaded":
        relative = record.get("file_path")
        if not isinstance(relative, str):
            raise ValueError(f"checkpoint {asset_id} has no file path")
        asset = (run_dir / relative).resolve()
        if not asset.is_relative_to((run_dir / "raw").resolve()) or not asset.is_file():
            raise ValueError(f"checkpoint asset is missing or unsafe: {relative}")
        size, digest = _hash_file(asset)
        if size != record.get("bytes") or digest != record.get("sha256"):
            raise ValueError(f"checkpoint asset was modified: {relative}")
    elif record.get("file_path") is not None:
        raise ValueError(f"non-download checkpoint has a file path: {asset_id}")
    if status in {"downloaded", "duplicate"} and (
        not isinstance(record.get("bytes"), int)
        or record["bytes"] < 0
        or not isinstance(record.get("sha256"), str)
        or not SHA256_RE.fullmatch(record["sha256"])
    ):
        raise ValueError(f"checkpoint {asset_id} has invalid content metadata")
    if status in {"skipped", "error"} and not isinstance(record.get("reason"), str):
        raise ValueError(f"checkpoint {asset_id} has no audit reason")


def _write_checkpoint(run_dir: Path, sequence: int, record: dict) -> None:
    write_json(
        _checkpoint_path(run_dir, record["asset_id"]),
        {"sequence": sequence, "record": record},
    )


def _invalidate_processing_outputs(run_dir: Path) -> None:
    (run_dir / "processed.jsonl").unlink(missing_ok=True)
    (run_dir / "processing_summary.json").unlink(missing_ok=True)
    derived = run_dir / "derived"
    if derived.exists():
        shutil.rmtree(derived)


def _record_event(events_path: Path, events: list[dict], event: dict) -> None:
    events.append({**event, "at": _utc_now()})
    write_jsonl(events_path, events)


def _base_record(candidate: Candidate, settings: Settings, source_index: int) -> dict:
    source = settings.sources[source_index]
    media_type, _ = mimetypes.guess_type(candidate.name)
    return {
        "asset_id": _candidate_id(candidate),
        "parent_id": None,
        "source_id": source.id,
        "source_adapter": source.adapter,
        "origin": candidate.origin,
        "original_name": candidate.name,
        "license": source.license,
        "rights_note": source.rights_note,
        "collected_at": _utc_now(),
        "mime_guess": media_type or "application/octet-stream",
        "media_kind": _kind(media_type),
        "review_status": "unreviewed",
        "label": None,
        "split": None,
    }


def collect(settings: Settings, run_dir: Path, *, resume: bool = False) -> dict:
    """Collect a new run, or resume one whose config fingerprint still matches."""
    run_dir = run_dir.resolve()
    state = _prepare_run(settings, run_dir, resume)
    checkpoints = _load_checkpoints(run_dir)
    rows = [record for _, record in checkpoints]
    by_asset_id = {record["asset_id"]: record for record in rows}
    if len(by_asset_id) != len(rows):
        raise ValueError("duplicate asset IDs in checkpoints")
    seen_hashes: dict[str, str] = {}
    for record in rows:
        _verify_checkpoint_record(run_dir, record)
        if record["status"] == "downloaded":
            seen_hashes.setdefault(record["sha256"], record["asset_id"])
    downloaded_by_id = {
        record["asset_id"]: record for record in rows if record["status"] == "downloaded"
    }
    for record in rows:
        if record["status"] != "duplicate":
            continue
        original = downloaded_by_id.get(record.get("duplicate_of"))
        if original is None or original["sha256"] != record["sha256"]:
            raise ValueError(
                f"checkpoint duplicate target is missing or inconsistent: {record['asset_id']}"
            )
    write_jsonl(run_dir / "manifest.jsonl", rows)

    events_path = run_dir / "events.jsonl"
    events = read_jsonl(events_path)
    terminal_sources = {
        event["source_id"] for event in events if event.get("status") in TERMINAL_SOURCE_STATUSES
    }
    next_sequence = max((sequence for sequence, _ in checkpoints), default=-1) + 1
    processing_invalidated = False

    with requests.Session() as session:
        session.trust_env = False  # avoid auto-enabling proxy/credential environments
        policy = WebPolicy(session, settings.limits.min_request_delay_seconds)
        for source_index, source in enumerate(settings.sources):
            if source.id in terminal_sources:
                continue
            if not source.enabled:
                _record_event(
                    events_path,
                    events,
                    {"source_id": source.id, "status": "skipped", "reason": "disabled"},
                )
                terminal_sources.add(source.id)
                continue
            if not source.rights_confirmed:
                _record_event(
                    events_path,
                    events,
                    {
                        "source_id": source.id,
                        "status": "skipped",
                        "reason": "rights_not_confirmed",
                    },
                )
                terminal_sources.add(source.id)
                continue
            previous_attempt = any(event.get("source_id") == source.id for event in events)
            _record_event(
                events_path,
                events,
                {
                    "source_id": source.id,
                    "status": "resumed" if previous_attempt else "started",
                    "adapter": source.adapter,
                },
            )
            try:
                for candidate in discover(source, settings.limits):
                    asset_id = _candidate_id(candidate)
                    if asset_id in by_asset_id:
                        continue
                    destination = (
                        run_dir / "raw" / source.id / f"{asset_id}{_name_ext(candidate.name)}"
                    )
                    item = _base_record(candidate, settings, source_index)
                    try:
                        skip_reason = candidate.meta.get("skip_reason")
                        if skip_reason:
                            raise SkipAsset(str(skip_reason))
                        if destination.exists():
                            size, digest = _hash_file(destination, settings.limits.max_bytes)
                        else:
                            size, digest = fetch(candidate, destination, settings.limits, policy)
                        item.update({"sha256": digest, "bytes": size})
                        if digest in seen_hashes:
                            item.update(
                                {
                                    "status": "duplicate",
                                    "duplicate_of": seen_hashes[digest],
                                    "file_path": None,
                                }
                            )
                            destination.unlink(missing_ok=True)
                        else:
                            seen_hashes[digest] = asset_id
                            item.update(
                                {
                                    "status": "downloaded",
                                    "file_path": destination.relative_to(run_dir).as_posix(),
                                }
                            )
                    except SkipAsset as exc:
                        item.update({"status": "skipped", "reason": str(exc), "file_path": None})
                    except Exception as exc:  # noqa: BLE001 -- intentional audit boundary
                        item.update(
                            {
                                "status": "error",
                                "reason": f"{type(exc).__name__}: {exc}",
                                "file_path": None,
                            }
                        )
                    if not processing_invalidated:
                        _invalidate_processing_outputs(run_dir)
                        processing_invalidated = True
                    _write_checkpoint(run_dir, next_sequence, item)
                    next_sequence += 1
                    rows.append(item)
                    by_asset_id[asset_id] = item
                    write_jsonl(run_dir / "manifest.jsonl", rows)
            except Exception as exc:  # noqa: BLE001 -- intentional audit boundary
                _record_event(
                    events_path,
                    events,
                    {
                        "source_id": source.id,
                        "status": "error",
                        "reason": f"{type(exc).__name__}: {exc}",
                    },
                )
                continue
            _record_event(
                events_path,
                events,
                {"source_id": source.id, "status": "completed", "adapter": source.adapter},
            )
            terminal_sources.add(source.id)

    summary = {
        "name": settings.name,
        "run_dir": str(run_dir),
        "downloaded": sum(row["status"] == "downloaded" for row in rows),
        "duplicate": sum(row["status"] == "duplicate" for row in rows),
        "skipped": sum(row["status"] == "skipped" for row in rows),
        "errors": sum(row["status"] == "error" for row in rows),
        "source_events": len(events),
        "resumed": resume,
        "created_at": state["created_at"],
        "completed_at": _utc_now(),
    }
    write_json(run_dir / "collection_summary.json", summary)
    state.update({"status": "completed", "updated_at": summary["completed_at"]})
    write_json(run_dir / "run_state.json", state)
    return summary


def _kind(mime: str | None) -> str:
    if not mime:
        return "binary"
    for kind in ("image", "video", "audio", "text"):
        if mime.startswith(kind + "/"):
            return kind
    if mime in {"application/pdf", "application/msword"}:
        return "document"
    if mime in {"application/json", "application/xml", "application/zip"}:
        return "structured_or_archive"
    return "binary"
