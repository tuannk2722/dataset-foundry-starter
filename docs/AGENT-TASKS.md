# Task Sequence for Codex / AI Agents

## Task 1 — Audit the scaffold

> Read AGENTS.md, README.md, docs/00-INDEX.md. Run the local offline smoke test and full test suite. Audit module boundaries and candidate-batch-v1 for correctness, security and extensibility. Fix bugs with tests. Do not introduce domain-specific labels or try remote collection. Report results and limitations, then commit.

## Task 2 — Improve reliability

> Add resumable collection using per-asset checkpoints + atomic manifest writes. Keep raw content immutable, preserve source evidence, check bytes and file counts, record skipped/blocked/error reasons. Do not bypass any access restriction. Write interruption/restart tests and update docs. Make one focused commit.

## Task 3 — Plugin architecture

> Refactor source discovery and processing into well-defined, tested adapter and processor protocols/registries. Preserve all v1 CLI commands and candidate-batch-v1 export compatibility. Demonstrate with one synthetic local plugin. No UI or machine-learning inference yet. Commit separately.

## Task 4 — Project-side integration (different repo)

> Inspect the existing AI Exam Monitoring project's import/audit contract. Implement a separate candidate-batch-v1 importer into a quarantine area. Validate ZIP safety, sha256, source rights, manifest, duplicates/grouping, and unreviewed status. Do not modify existing train/val/test or frozen holdout unless explicitly approved after review. Preserve the generic collector as an independent repo.

## Agent collaboration

Subagents can work independently on tests/adapters/docs, but the lead agent must own compatibility, review security-sensitive edits and run integration tests. Avoid parallel edits to shared schema/config/pipeline files.
