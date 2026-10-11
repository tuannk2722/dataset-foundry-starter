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

## v0.3 — implemented

- Per-source byte quotas, bounded parallel fetches, progress callbacks and transient retry.
- Content-signature/MIME policy, exact cross-run SQLite index and package entry-point plugins.
- Approved-page direct/browser engines with fail-closed challenge detection and no bypass.
- Local token-protected review UI plus source health events and structured alert artifacts.

## Recommended next

- Production hardening for metrics/alert exporters, controlled egress and long-running jobs.
- Broader content-signature coverage and optional full decoder validation profiles.
- More reliable SSRF/DNS pinning or controlled egress when used with untrusted URL inputs.
- Review audit-history export under a separately versioned contract if downstream needs it.

## Later — only as needed

- Additional approved provider adapters, e.g. Roboflow SDK.
- Configurable processing profiles: image crops, text document extraction, audio segmentation/transcription, tabular dataset normalization.
- DVC-compatible artifact storage and batch versioning.
- Optional task-specific adapters: map generic candidates to AI Exam Monitoring staging schema in a separate integration package. No target-specific logic in core.
