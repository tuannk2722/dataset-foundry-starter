"""Candidate batch export; NOT a training dataset and never a frozen holdout."""
import json
from collections import Counter
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .manifest import read_jsonl

SCHEMA_VERSION = "candidate-batch-v1"


def export_batch(run_dir: Path, output: Path) -> dict:
    run_dir = run_dir.resolve()
    output = output.resolve()
    if output.is_relative_to(run_dir):
        raise ValueError("Write exports outside the run directory")
    data_path = run_dir / "processed.jsonl"
    if not data_path.exists():
        data_path = run_dir / "manifest.jsonl"
    records = read_jsonl(data_path)
    included = [r for r in records if r["status"] in {"downloaded", "derived"}]
    counts = Counter(r["media_kind"] for r in included)
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED, compresslevel=4) as zf:
        for record in included:
            rel = record["file_path"]
            asset = (run_dir / rel).resolve()
            if not asset.is_relative_to(run_dir) or not asset.is_file():
                raise ValueError(f"Missing or unsafe asset: {rel}")
            zf.write(asset, f"assets/{rel}")
        zf.writestr("manifest.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
        for extra in ("sources.json", "collection_summary.json", "processing_summary.json", "events.jsonl"):
            if (run_dir / extra).is_file():
                zf.write(run_dir / extra, extra)
        zf.writestr("schema.json", json.dumps({
            "schema_version": SCHEMA_VERSION,
            "kind": "unreviewed_candidates", "asset_path_prefix": "assets/",
            "split_assigned": False, "labels_verified": False,
        }, indent=2))
        zf.writestr("README.txt", (
            "DATASET FOUNDRY CANDIDATE BATCH v1\n"
            "These files are unreviewed candidates, NOT a training/validation/test dataset.\n"
            "Review rights, provenance, quality, subject privacy, duplicates, and labels\n"
            "before promotion into a project's versioned datasets.\n"
            "file_path in manifest.jsonl refers to the path AFTER assets/.\n"
        ))
    return {"output": str(output), "exported_assets": len(included), "media_kinds": dict(counts)}
