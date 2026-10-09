"""Collection orchestrator. Sequential and bounded by design for 8GB laptops."""
from __future__ import annotations

import hashlib
import mimetypes
from datetime import UTC, datetime
from pathlib import Path

import requests

from .adapters.discovery import discover
from .downloader import fetch
from .manifest import write_json, write_jsonl
from .models import Candidate, Settings
from .policy import SkipAsset, WebPolicy


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


def collect(settings: Settings, run_dir: Path) -> dict:
    run_dir = run_dir.resolve()
    if (run_dir / "manifest.jsonl").exists():
        raise FileExistsError(f"run already exists: {run_dir}. Choose another run directory.")
    run_dir.mkdir(parents=True, exist_ok=True)
    with requests.Session() as session:
        session.trust_env = False  # avoid auto-enabling proxy/credential environments
        policy = WebPolicy(session, settings.limits.min_request_delay_seconds)
        rows, events, seen_hashes = [], [], {}
        for source in settings.sources:
            if not source.enabled:
                events.append({"source_id": source.id, "status": "skipped", "reason": "disabled"})
                continue
            if not source.rights_confirmed:
                events.append({"source_id": source.id, "status": "skipped", "reason": "rights_not_confirmed"})
                continue
            events.append({"source_id": source.id, "status": "started", "adapter": source.adapter})
            try:
                for candidate in discover(source, settings.limits):
                    asset_id = _candidate_id(candidate)
                    destination = run_dir / "raw" / source.id / f"{asset_id}{_name_ext(candidate.name)}"
                    media_type, _ = mimetypes.guess_type(candidate.name)
                    item = {
                        "asset_id": asset_id,
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
                    try:
                        size, digest = fetch(candidate, destination, settings.limits, policy)
                        item.update({"sha256": digest, "bytes": size})
                        if digest in seen_hashes:
                            item.update({
                                "status": "duplicate", "duplicate_of": seen_hashes[digest],
                                "file_path": None,
                            })
                            destination.unlink(missing_ok=True)
                        else:
                            seen_hashes[digest] = asset_id
                            item.update({
                                "status": "downloaded",
                                "file_path": destination.relative_to(run_dir).as_posix(),
                            })
                    except SkipAsset as exc:
                        item.update({"status": "skipped", "reason": str(exc), "file_path": None})
                    except Exception as exc:  # noqa: BLE001 -- intentional audit boundary
                        item.update({"status": "error", "reason": f"{type(exc).__name__}: {exc}", "file_path": None})
                    rows.append(item)
            except Exception as exc:  # noqa: BLE001 -- intentional audit boundary
                events.append({
                    "source_id": source.id, "status": "error", "reason": f"{type(exc).__name__}: {exc}"
                })
        write_jsonl(run_dir / "manifest.jsonl", rows)
        write_jsonl(run_dir / "events.jsonl", events)
        write_json(run_dir / "sources.json", [
            {
                "id": s.id, "adapter": s.adapter, "enabled": s.enabled,
                "rights_confirmed": s.rights_confirmed, "license": s.license,
                "rights_note": s.rights_note,
            }
            for s in settings.sources
        ])
        summary = {
            "name": settings.name, "run_dir": str(run_dir),
            "downloaded": sum(x["status"] == "downloaded" for x in rows),
            "duplicate": sum(x["status"] == "duplicate" for x in rows),
            "skipped": sum(x["status"] == "skipped" for x in rows),
            "errors": sum(x["status"] == "error" for x in rows),
            "source_events": len(events),
            "created_at": _utc_now(),
        }
        write_json(run_dir / "collection_summary.json", summary)
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
