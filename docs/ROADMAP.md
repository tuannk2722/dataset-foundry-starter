# Roadmap (scope is intentionally generic)

## v0.1 — implemented

- TOML registry, rights gate, local directory / local ZIP / static HTTPS URLs / optional HF dataset adapter.
- Sequential bounded file collection, source events, SHA-256 exact duplicates, optional image metadata/aHash, optional FFmpeg video frame derivation.
- Manifest and `candidate-batch-v1` export; basic offline tests.

## v0.2 — recommended next

- Crash-resume/checkpointing and source level quotas, timestamps, progress reports.
- Processor Protocol / entry points (source discovery already has a registry); robust producer/worker queue for carefully bounded parallelism.
- Configurable media validation policies, source-specific allowed MIME, content signatures and file-type mismatch detection.
- More reliable SS RF/DNS pinning or controlled egress when used with untrusted URL inputs.
- Hash/index across multiple runs and a review UI for generic acceptance/rejection.

## v0.3+ — only as needed

- Additional approved provider adapters, e.g. Roboflow SDK.
- Configurable processing profiles: image crops, text document extraction, audio segmentation/transcription, tabular dataset normalization.
- DVC-compatible artifact storage and batch versioning.
- Optional task-specific adapters: map generic candidates to AI Exam Monitoring staging schema in a separate integration package. No target-specific logic in core.
