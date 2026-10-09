# Architecture

## Boundaries

`config.py` parses TOML -> immutable `Settings`. `adapters/discovery.py` registers focused adapter modules that yield `Candidate`; `downloader.py` accepts candidate and writes bounded stream, `policy.py` handles HTTPS/robots/stop conditions. `pipeline.py` owns source schedule and raw manifest. `processors.py` enriches/reuses the manifest. `exporter.py` emits `candidate-batch-v1`. `cli.py` is the entry point.

Generic candidates have `source_id`, `kind`, `origin`, `name`, and adapter-specific metadata. Any source may contain images, audio, video, text, PDF, CSV, or other files. **Being collectable does not mean it has been decoded or semantically understood.**

## Add a new adapter

1. Extend the configuration adapter vocabulary in `config.py` and `models.py` only if extra settings are needed.
2. Add candidate discovery and an explicit fetch strategy; preserve bounded streaming and provenance.
3. If the provider has an official export/download API, prefer it to page parsing.
4. Add integration tests for inaccessible/gated/rate-limited sources.
5. Document required rights/license checks; leave disabled in example config.

A small `DiscoveryAdapter` Protocol and adapter registry are already present. Add a module for each new source type, then register its discovery function. Download handling remains centralized in `downloader.py`, so a new transport kind needs a matching bounded fetcher.

## Add a processor

A processor consumes `run_dir` and manifest rows, writes derived artifacts under `derived/`, and records `asset_id`, `parent_id`, `source_id`, `origin`, `review_status`, and file checksum. Never overwrite original files. Prefer a registry of processor stages over adding task-specific branches to the generic core. Example future plugins: audio metadata, PDF text extraction, table schema validation, object detection/crop with configurable target ontology.

## Run durability

The v0.1 `collect` uses an immutable-new-run convention, not crash-resume. Interrupted collection may leave some `raw/` files without a written manifest because the manifest is committed at the end. Production enhancement: append-only event log + checkpoint and replay; atomic per-asset commit; retry state. Do not claim this is already implemented.

## Data architecture

Separate `workspace` (private working copies) from `exports` (candidate review packages). No model training or evaluation in this repository; no dependencies on AI Exam Monitoring code. A downstream importer owns mapping, audit, cross-dataset near-duplicates, human label decisions, versioning, split groups and frozen holdouts.
