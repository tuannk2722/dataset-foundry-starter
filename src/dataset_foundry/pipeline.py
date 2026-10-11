"""Crash-resumable collection orchestrator, sequential and bounded by design."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import requests

from .adapters.discovery import discover
from .content_types import mime_allowed, mime_types_compatible, read_bounded_prefix, sniff_mime
from .downloader import AssetTooLarge, SourceByteQuota, fetch
from .hash_index import HashIndex
from .manifest import read_json, read_jsonl, write_json, write_jsonl
from .models import Candidate, Settings
from .policy import SkipAsset, WebPolicy

RUN_STATE_VERSION = "dataset-foundry-run-v1"
TERMINAL_SOURCE_STATUSES = {"completed", "degraded", "skipped"}
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
            "allowed_mime_types": list(source.allowed_mime_types),
            "render_engine": source.render_engine,
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


def _emit_progress(callback: Callable[[dict], None] | None, event: dict) -> None:
    if callback is not None:
        callback(event)


def _validate_downloaded_content(
    destination: Path,
    expected_mime: str,
    settings: Settings,
    source_index: int,
    candidate_kind: str,
) -> tuple[str | None, str]:
    source = settings.sources[source_index]
    with destination.open("rb") as handle:
        detected = sniff_mime(read_bounded_prefix(handle))
    if candidate_kind == "http" and detected == "text/html" and expected_mime != "text/html":
        raise SkipAsset("unexpected HTML response for a direct asset URL")
    try:
        compatible = detected is None or mime_types_compatible(expected_mime, detected)
        allowed = mime_allowed(detected, source.allowed_mime_types)
    except ValueError as exc:
        raise SkipAsset(f"invalid MIME declaration: {exc}") from exc
    if not allowed:
        raise SkipAsset(
            f"detected MIME {detected or 'unknown'} is not allowed for source {source.id}"
        )
    validation = "matched" if compatible else "mismatch"
    if not compatible and settings.limits.mime_validation == "strict":
        raise SkipAsset(
            f"content signature mismatch: expected {expected_mime}, "
            f"detected {detected or 'unknown'}"
        )
    if settings.limits.mime_validation == "off":
        validation = "off"
    return detected, validation


def _fetch_candidate(
    candidate: Candidate,
    destination: Path,
    settings: Settings,
    source_index: int,
    policy: WebPolicy,
    quota: SourceByteQuota,
) -> dict:
    skip_reason = candidate.meta.get("skip_reason")
    if skip_reason:
        raise SkipAsset(str(skip_reason))
    reserved = False
    size = 0
    try:
        if destination.exists():
            size, digest = _hash_file(destination, settings.limits.max_bytes)
            quota.reserve(size)
            reserved = True
        else:
            size, digest = fetch(candidate, destination, settings.limits, policy, quota)
            reserved = True
        guessed_mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        declared_mime = str(candidate.meta.get("content_type", "")).partition(";")[0].strip()
        expected_mime = declared_mime or guessed_mime
        detected, validation = _validate_downloaded_content(
            destination, expected_mime, settings, source_index, candidate.kind
        )
        return {
            "bytes": size,
            "sha256": digest,
            "mime_detected": detected,
            "mime_declared": declared_mime or None,
            "mime_validation": validation,
            "attempts": int(candidate.meta.get("attempts", 1)),
        }
    except BaseException:
        if reserved:
            quota.release(size)
        destination.unlink(missing_ok=True)
        raise


def _bounded_fetches(
    candidates: Iterator[Candidate],
    destination_for: Callable[[Candidate], Path],
    settings: Settings,
    source_index: int,
    policy: WebPolicy,
    quota: SourceByteQuota,
) -> Iterator[tuple[Candidate, Path, Future]]:
    """Submit at most max_workers tasks and yield futures in discovery order."""
    pending: deque[tuple[Candidate, Path, Future]] = deque()
    completed = False
    try:
        with ThreadPoolExecutor(
            max_workers=settings.limits.max_workers,
            thread_name_prefix="dsforge-fetch",
        ) as executor:

            def submit_one() -> bool:
                try:
                    candidate = next(candidates)
                except StopIteration:
                    return False
                destination = destination_for(candidate)
                future = executor.submit(
                    _fetch_candidate,
                    candidate,
                    destination,
                    settings,
                    source_index,
                    policy,
                    quota,
                )
                pending.append((candidate, destination, future))
                return True

            for _ in range(settings.limits.max_workers):
                if not submit_one():
                    break
            while pending:
                current = pending.popleft()
                yield current
                submit_one()
        completed = True
    finally:
        if not completed:
            for _candidate, destination, future in pending:
                future.cancel()
                destination.unlink(missing_ok=True)


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


def collect(
    settings: Settings,
    run_dir: Path,
    *,
    resume: bool = False,
    progress: Callable[[dict], None] | None = None,
    hash_index_path: Path | None = None,
) -> dict:
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
        if record.get("duplicate_scope") == "cross_run":
            if not isinstance(record.get("duplicate_run"), str):
                raise ValueError(
                    f"checkpoint cross-run duplicate has no run reference: {record['asset_id']}"
                )
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
    alerts_path = run_dir / "alerts.jsonl"
    alerts = read_jsonl(alerts_path) if alerts_path.exists() else []
    index_path = (
        hash_index_path.resolve()
        if hash_index_path is not None
        else run_dir.parent / ".dataset-foundry-hashes.sqlite3"
    )

    def record_alert(source_id: str, item: dict) -> None:
        reason = str(item.get("reason", ""))
        category = next(
            (
                category
                for pattern, category in (
                    ("captcha_required", "captcha_required"),
                    ("bot_challenge", "bot_challenge"),
                    ("rate_limited", "rate_limited"),
                    ("remote source blocked or limited", "access_denied"),
                    ("remote access blocked", "access_denied"),
                    ("browser access blocked", "access_denied"),
                    ("robots.txt", "robots_policy"),
                    ("source exceeds maximum bytes", "source_quota_exceeded"),
                    ("HTTP request failed after", "retry_exhausted"),
                )
                if pattern in reason
            ),
            None,
        )
        if category is None:
            return
        alerts.append(
            {
                "at": _utc_now(),
                "source_id": source_id,
                "asset_id": item["asset_id"],
                "category": category,
                "reason": reason,
            }
        )
        write_jsonl(alerts_path, alerts)

    with HashIndex(index_path) as hash_index, requests.Session() as session:
        session.trust_env = False  # avoid auto-enabling proxy/credential environments
        policy = WebPolicy(session, settings.limits.min_request_delay_seconds)
        for record in rows:
            if record["status"] == "downloaded":
                hash_index.register(
                    record["sha256"],
                    record["bytes"],
                    run_dir / record["file_path"],
                    run_id=str(run_dir),
                    asset_id=record["asset_id"],
                )
        _emit_progress(
            progress,
            {"phase": "collect", "status": "started", "sources_total": len(settings.sources)},
        )
        for source_index, source in enumerate(settings.sources):
            if source.id in terminal_sources:
                _emit_progress(
                    progress,
                    {
                        "phase": "source",
                        "status": "already_complete",
                        "source_id": source.id,
                        "source_index": source_index + 1,
                        "sources_total": len(settings.sources),
                    },
                )
                continue
            if not source.enabled:
                _record_event(
                    events_path,
                    events,
                    {"source_id": source.id, "status": "skipped", "reason": "disabled"},
                )
                terminal_sources.add(source.id)
                _emit_progress(
                    progress,
                    {
                        "phase": "source",
                        "status": "skipped",
                        "source_id": source.id,
                        "source_index": source_index + 1,
                        "sources_total": len(settings.sources),
                    },
                )
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
                _emit_progress(
                    progress,
                    {
                        "phase": "source",
                        "status": "skipped",
                        "source_id": source.id,
                        "source_index": source_index + 1,
                        "sources_total": len(settings.sources),
                    },
                )
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
            source_row_start = len(rows)
            collected_bytes = sum(
                record.get("bytes", 0)
                for record in rows
                if record.get("source_id") == source.id
                and record.get("status") in {"downloaded", "duplicate"}
            )
            quota = SourceByteQuota(
                settings.limits.max_source_mb * 1024 * 1024, collected_bytes
            )
            try:
                candidates = (
                    candidate
                    for candidate in discover(source, settings.limits)
                    if _candidate_id(candidate) not in by_asset_id
                )

                def destination_for(
                    candidate: Candidate, source_id: str = source.id
                ) -> Path:
                    asset_id = _candidate_id(candidate)
                    return run_dir / "raw" / source_id / f"{asset_id}{_name_ext(candidate.name)}"

                for candidate, destination, future in _bounded_fetches(
                    iter(candidates),
                    destination_for,
                    settings,
                    source_index,
                    policy,
                    quota,
                ):
                    asset_id = _candidate_id(candidate)
                    item = _base_record(candidate, settings, source_index)
                    try:
                        result = future.result()
                        size = result["bytes"]
                        digest = result["sha256"]
                        item.update(result)
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
                            indexed = hash_index.lookup(digest, expected_bytes=size)
                            if indexed is not None:
                                item.update(
                                    {
                                        "status": "duplicate",
                                        "duplicate_of": indexed.asset_id,
                                        "duplicate_scope": "cross_run",
                                        "duplicate_run": indexed.run_id,
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
                        item.update(
                            {
                                "status": "skipped",
                                "reason": str(exc),
                                "file_path": None,
                                "attempts": int(candidate.meta.get("attempts", 1)),
                            }
                        )
                    except Exception as exc:  # noqa: BLE001 -- intentional audit boundary
                        item.update(
                            {
                                "status": "error",
                                "reason": f"{type(exc).__name__}: {exc}",
                                "file_path": None,
                                "attempts": int(candidate.meta.get("attempts", 1)),
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
                    if item["status"] == "downloaded":
                        hash_index.register(
                            item["sha256"],
                            item["bytes"],
                            destination,
                            run_id=str(run_dir),
                            asset_id=asset_id,
                        )
                    record_alert(source.id, item)
                    _emit_progress(
                        progress,
                        {
                            "phase": "asset",
                            "status": item["status"],
                            "source_id": source.id,
                            "processed": len(rows) - source_row_start,
                            "limit": settings.limits.max_assets_per_source,
                            "collected_bytes": quota.used,
                            "byte_limit": quota.limit,
                        },
                    )
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
                _emit_progress(
                    progress,
                    {
                        "phase": "source",
                        "status": "error",
                        "source_id": source.id,
                        "source_index": source_index + 1,
                        "sources_total": len(settings.sources),
                    },
                )
                continue
            source_rows = rows[source_row_start:]
            source_status = (
                "degraded"
                if any(row["status"] in {"skipped", "error"} for row in source_rows)
                else "completed"
            )
            _record_event(
                events_path,
                events,
                {
                    "source_id": source.id,
                    "status": source_status,
                    "adapter": source.adapter,
                    "collected_bytes": quota.used,
                },
            )
            terminal_sources.add(source.id)
            _emit_progress(
                progress,
                {
                    "phase": "source",
                    "status": source_status,
                    "source_id": source.id,
                    "source_index": source_index + 1,
                    "sources_total": len(settings.sources),
                },
            )

    summary = {
        "name": settings.name,
        "run_dir": str(run_dir),
        "downloaded": sum(row["status"] == "downloaded" for row in rows),
        "duplicate": sum(row["status"] == "duplicate" for row in rows),
        "skipped": sum(row["status"] == "skipped" for row in rows),
        "errors": sum(row["status"] == "error" for row in rows),
        "source_events": len(events),
        "degraded_sources": sum(event.get("status") == "degraded" for event in events),
        "failed_sources": sum(event.get("status") == "error" for event in events),
        "alerts": len(alerts),
        "collected_bytes": sum(
            row.get("bytes", 0)
            for row in rows
            if row["status"] in {"downloaded", "duplicate"}
        ),
        "resumed": resume,
        "created_at": state["created_at"],
        "completed_at": _utc_now(),
    }
    write_json(run_dir / "collection_summary.json", summary)
    final_status = (
        "degraded"
        if summary["errors"]
        or summary["alerts"]
        or summary["degraded_sources"]
        or summary["failed_sources"]
        else "completed"
    )
    state.update({"status": final_status, "updated_at": summary["completed_at"]})
    write_json(run_dir / "run_state.json", state)
    _emit_progress(progress, {"phase": "collect", "status": final_status, **summary})
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
