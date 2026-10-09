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

A small `DiscoveryAdapter` Protocol and adapter registry are present. Add a module for each
new source type, then call `register_adapter` before loading its configuration. Registration
is in-process; automatic Python package entry-point loading is not implemented. Download
handling remains centralized in `downloader.py`, so a new transport kind needs a matching
bounded fetcher.

## Add a processor

A processor implements the `Processor` protocol, consumes a record plus immutable
`ProcessingContext`, and may enrich the record or return derived records under `derived/`.
Register it with `register_processor`. The pipeline validates protected raw provenance,
parent IDs, safe derived paths, checksums, and unreviewed/unlabeled/unsplit state before
atomically replacing `processed.jsonl`. Example future plugins: audio metadata, PDF text
extraction, table schema validation, or configurable generic crops.

## Run durability

Fresh collection still requires an empty run directory. Before each candidate becomes
visible in `manifest.jsonl`, collection atomically commits a per-asset checkpoint.
`--resume` verifies the effective config fingerprint, reconstructs the manifest from
checkpoints, verifies checkpointed raw bytes, and recovers a complete raw file that was
written immediately before an interruption. Source lifecycle events distinguish started,
resumed, completed, skipped and error outcomes. There is no automatic retry/backoff queue;
an operator explicitly invokes `--resume`.

If a resumed collection commits any new candidate outcome, stale `processed.jsonl`,
`processing_summary.json` and `derived/` outputs are invalidated. A no-op resume of a
completed run preserves them.

JSON/JSONL metadata and the final export ZIP use sibling temporary files plus atomic
replacement. Raw assets are streamed to a temporary directory and moved into place only
after a complete bounded copy.

## Data architecture

Separate `workspace` (private working copies) from `exports` (candidate review packages). No model training or evaluation in this repository; no dependencies on AI Exam Monitoring code. A downstream importer owns mapping, audit, cross-dataset near-duplicates, human label decisions, versioning, split groups and frozen holdouts.
