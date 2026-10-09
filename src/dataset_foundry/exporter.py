"""Candidate batch export; NOT a training dataset and never a frozen holdout."""

import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .manifest import read_jsonl

SCHEMA_VERSION = "candidate-batch-v1"
CONTRACT_STATUSES = {"downloaded", "derived", "duplicate", "skipped", "error"}


def _verify_asset(path: Path, record: dict) -> None:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    if size != record.get("bytes") or digest.hexdigest() != record.get("sha256"):
        raise ValueError(f"Asset checksum mismatch: {record.get('file_path')}")


def _validate_contract_record(record: dict) -> None:
    asset_id = record.get("asset_id")
    if record.get("status") not in CONTRACT_STATUSES:
        raise ValueError(f"Invalid manifest status for asset {asset_id}")
    if (
        record.get("review_status") != "unreviewed"
        or record.get("label") is not None
        or record.get("split") is not None
    ):
        raise ValueError(f"Manifest contains promoted review data for asset {asset_id}")


def export_batch(run_dir: Path, output: Path) -> dict:
    run_dir = run_dir.resolve()
    output = output.resolve()
    if output.is_relative_to(run_dir):
        raise ValueError("Write exports outside the run directory")
    data_path = run_dir / "processed.jsonl"
    if not data_path.exists():
        data_path = run_dir / "manifest.jsonl"
    records = read_jsonl(data_path)
    for record in records:
        _validate_contract_record(record)
    included = [r for r in records if r["status"] in {"downloaded", "derived"}]
    counts = Counter(r["media_kind"] for r in included)
    assets = []
    for record in included:
        rel = record["file_path"]
        asset = (run_dir / rel).resolve()
        if not asset.is_relative_to(run_dir) or not asset.is_file():
            raise ValueError(f"Missing or unsafe asset: {rel}")
        _verify_asset(asset, record)
        assets.append((record, asset))

    output.parent.mkdir(parents=True, exist_ok=True)
    pending: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output.parent, prefix=f".{output.name}.", suffix=".tmp", delete=False
        ) as handle:
            pending = Path(handle.name)
        with ZipFile(pending, "w", ZIP_DEFLATED, compresslevel=4) as zf:
            for record, asset in assets:
                zf.write(asset, f"assets/{record['file_path']}")
            zf.writestr(
                "manifest.jsonl",
                "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            )
            for extra in (
                "sources.json",
                "collection_summary.json",
                "processing_summary.json",
                "events.jsonl",
            ):
                if (run_dir / extra).is_file():
                    zf.write(run_dir / extra, extra)
            zf.writestr(
                "schema.json",
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "kind": "unreviewed_candidates",
                        "asset_path_prefix": "assets/",
                        "split_assigned": False,
                        "labels_verified": False,
                    },
                    indent=2,
                ),
            )
            zf.writestr(
                "README.txt",
                "DATASET FOUNDRY CANDIDATE BATCH v1\n"
                "These files are unreviewed candidates, NOT a training/validation/test dataset.\n"
                "Review rights, provenance, quality, subject privacy, duplicates, and labels\n"
                "before promotion into a project's versioned datasets.\n"
                "file_path in manifest.jsonl refers to the path AFTER assets/.\n",
            )
        os.replace(pending, output)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return {"output": str(output), "exported_assets": len(included), "media_kinds": dict(counts)}
