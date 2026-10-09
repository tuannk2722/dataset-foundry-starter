# Roadmap (scope is intentionally generic)

## v0.1 — implemented

- TOML registry, rights gate, local directory / local ZIP / static HTTPS URLs / optional HF dataset adapter.
- Sequential bounded file collection, source events, SHA-256 exact duplicates, optional image metadata/aHash, optional FFmpeg video frame derivation.
- Manifest and `candidate-batch-v1` export; basic offline tests.

## v0.2 — implemented

- Crash resume with effective-config fingerprinting, atomic per-asset checkpoints and
  metadata replacement, orphan raw recovery, and checkpoint checksum verification.
- Tested discovery and processor protocols/registries with a synthetic local plugin.
- Processor invariant validation prevents provenance mutation or silent label/review/split
  promotion.
- Atomic export with pre-export size/SHA-256 verification.
- Fail-closed typed TOML validation and auditable reasons for rejected ZIP members.

## Recommended next

- Source-level byte quotas, progress reporting, explicit retry policy, and a robust
  producer/worker queue for carefully bounded parallelism.
- Optional Python package entry-point loading for the existing in-process registries.
- Configurable media validation policies, source-specific allowed MIME, content signatures and file-type mismatch detection.
- More reliable SSRF/DNS pinning or controlled egress when used with untrusted URL inputs.
- Hash/index across multiple runs and a review UI for generic acceptance/rejection.

## Later — only as needed

- Additional approved provider adapters, e.g. Roboflow SDK.
- Configurable processing profiles: image crops, text document extraction, audio segmentation/transcription, tabular dataset normalization.
- DVC-compatible artifact storage and batch versioning.
- Optional task-specific adapters: map generic candidates to AI Exam Monitoring staging schema in a separate integration package. No target-specific logic in core.
