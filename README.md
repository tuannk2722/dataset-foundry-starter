# Dataset Foundry Starter

A small **general-purpose multimodal candidate collector** (Python 3.11+) intended to be extended by AI coding agents. It does **not** train models or claim that acquired data is correctly labeled.

**Maturity:** operational local-first collector, not a general-purpose unrestricted crawler.
It supports local folders, ZIP archives, static HTTPS asset URLs, approved HTML pages, and
an optional Hugging Face dataset adapter. Collection has bounded parallel workers,
per-source byte quotas, retry/backoff for transient failures, per-asset checkpoints, atomic
metadata writes, verified crash resume, cross-run exact deduplication, content-signature
validation, progress reporting, and immutable raw files. Generic preprocessing covers
image quality metadata (optional Pillow), optional video frame extraction (external
FFmpeg), and in-process adapter/processor registries. Audio/documents are ingested and
tagged but not decoded/transcribed yet. No web page discovery/spidering, semantic labeling,
No Roboflow adapter or trained dataset promotion is implemented. A token-protected local review
UI records human decisions in a sidecar database without promoting labels or changing v1.

## 1. Install (Windows PowerShell)

```powershell
cd D:\dataset-foundry-starter
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[images,huggingface,dev]"
```

You may install only `-e .` if you only need local/ZIP/HTTPS collection without image metadata and Hugging Face. For `--video-interval`, install FFmpeg and add it to PATH. Use **D:** for workspace/cache if C: is low on space.

For approved JavaScript-rendered pages, install `-e ".[browser]"` and then run
`playwright install chromium`. Browser mode is optional and disabled unless a `web_page`
source selects it.

## 2. Offline smoke test (no network)

```powershell
.\.venv\Scripts\dsforge.exe validate --config configs/sources.example.toml
.\.venv\Scripts\dsforge.exe collect --config configs/sources.example.toml --run workspace/demo-001
.\.venv\Scripts\dsforge.exe process --run workspace/demo-001
.\.venv\Scripts\dsforge.exe export --run workspace/demo-001 --output exports/demo-001.zip
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

If collection is interrupted, resume the same run with exactly the same effective
configuration:

```powershell
.\.venv\Scripts\dsforge.exe collect --config configs/sources.example.toml --run workspace/demo-001 --resume
```

Alternatively (without installing), run commands using `PYTHONPATH=src python -m dataset_foundry ...` with `requests` installed.

The enabled demo source contains synthetic files created with the starter. Network sources are **disabled** until their rights and terms are checked.

## 3. Add a source

Copy `configs/sources.example.toml` to `configs/sources.local.toml` and edit it. Set `rights_confirmed = true`, a meaningful `license` and `rights_note`, and `enabled = true` only after verifying authorization. Paths are relative to the TOML file.

Example: importing an already downloaded dataset ZIP:

```toml
[[sources]]
id = "dataset-local-2026"
adapter = "local_zip"
enabled = true
rights_confirmed = true
license = "permission-documented"
rights_note = "Approved for the project's intended internal research; verify details independently."
path = "../imports/dataset.zip"
include_extensions = [".jpg", ".jpeg", ".png", ".txt", ".json"]
```

Example: a list of specific downloadable HTTPS files, **not a website crawl**:

```toml
[[sources]]
id = "approved-direct-assets"
adapter = "http_list"
enabled = true
rights_confirmed = true
license = "source-license"
rights_note = "Approved file URLs; site terms checked."
urls = ["https://approved.example/data/image1.jpg"]
```

For Hugging Face, `adapter = "hf_dataset"`, plus `repo_id`, `revision`, `include_extensions`. The adapter uses public API access by default (no credential/policy bypass). If a dataset is gated or unavailable, leave it disabled rather than trying to circumvent access restrictions.

## 4. Data flow

```text
configs/*.toml
    | validate rights metadata / explicit enable
    v
registered source adapters (local_dir, local_zip, http_list, hf_dataset)
    | discover bounded candidates
    v
collector: bounded streaming + checkpoints + sha256 exact dedup
    -> workspace/RUN/raw/ + manifest.jsonl
    | optional
    v
processor: image probe/aHash + optional FFmpeg frames -> processed.jsonl
    |
    v
exporter -> exports/RUN.zip (unreviewed candidates + source metadata)
    |
    v
external project staging -> task-specific labeling, perceptual dedup, train/val/test split
```

**Do not treat exported files as a labeled or split-ready dataset.** They intentionally keep `review_status=unreviewed`, `label=null`, `split=null`.

## 5. Safety and blocked sources

- A source without confirmed rights is skipped, and an event is recorded.
- Only explicitly configured HTTPS assets/pages are fetched; HTTP, credential-bearing URLs,
  internal/private IPs and DNS mappings are refused.
- `robots.txt` is respected by HTTP adapters; on errors it fails closed. A 401, 403, 429
  or 451 response, or a bounded response prefix indicating a Cloudflare/Akamai/Imperva/
  DataDome-style challenge or CAPTCHA, opens the host circuit and records an alert. Vendor
  detection is heuristic and is not proof that an IP address was blocked. **No CAPTCHA
  solving, proxy rotation, login bypass, stealth automation or rate-limit evasion.**
- Automatic retries apply only to transient network errors and 408/425/5xx responses.
  Access denials, robots exclusions, redirects, challenges and CAPTCHAs never trigger a
  browser fallback.
- Redirects are never auto-followed. Verify and add any approved destination explicitly.
- File size, files per source, ZIP member size and compression ratio are bounded. Untrusted ZIP contents are read by entry, never mass-extracted. Unsafe, encrypted, oversized, symlink and compression-ratio-rejected ZIP members are recorded as skipped with a reason.
- Collect only data you have permission to use, apply privacy minimization, and assess personal data/image rights (especially classrooms and minors). Site access permission and training-use permission are different questions.
- Avoid collecting passwords/tokens, sensitive personal data or private communications. Config files containing secret data should not be committed.

This is a protective baseline, not a formal legal/compliance or hardened network security audit.

## 6. Key output files

- `workspace/RUN/manifest.jsonl`: all candidate outcomes, including skipped, duplicate and error.
- `workspace/RUN/events.jsonl`: source-level events.
- `workspace/RUN/alerts.jsonl`: policy/quota/retry alerts when any are raised.
- `workspace/RUN/run_state.json`: run/config fingerprint and lifecycle state.
- `workspace/RUN/checkpoints/*.json`: atomic per-candidate recovery records.
- `workspace/RUN/processed.jsonl`: optional enrichment and derived frames.
- `workspace/RUN/sources.json`: source attribution and rights notes.
- `workspace/RUN/review.sqlite3`: optional human review decisions, separate from manifests.
- `workspace/.dataset-foundry-hashes.sqlite3`: exact cross-run SHA-256 index.
- `exports/RUN.zip`: portable **candidate-batch-v1** contract; see `docs/EXPORT-CONTRACT.md`.

The output directory for a fresh `collect` must be empty. This prevents accidental raw-data
overwrites. Use `--resume` only for that same run and configuration; existing checkpoint
assets are re-hashed before reuse, while an asset written immediately before interruption
is recovered without being overwritten. Repeated `process` regenerates the derived folder
and atomically replaces the processed manifest. Export re-verifies every included asset's
size and SHA-256 before atomically replacing the output ZIP. If resume commits a new
candidate, stale processed/derived outputs are removed and must be regenerated.

## 7. Extension registries

Discovery adapters implement `DiscoveryAdapter` and can be registered with
`register_adapter(name, callable)` before loading configuration. Processor stages implement
`Processor` and can be registered with `register_processor(name, callable)`; a stage may
enrich metadata or return derived records. Processor output is rejected if it changes raw
provenance, silently assigns review/label/split state, references an unsafe path, or reports
an incorrect checksum. See `tests/test_plugins.py` for a synthetic local example.

Installed trusted packages may also expose entry points in `dataset_foundry.adapters` or
`dataset_foundry.processors`; entry-point names become registry names. Collisions and
non-callable plugins fail closed. Loading an entry point executes package code, so only
install trusted plugins. Select plugin processors with repeated `--processor NAME` options.
Plugin packages declare callables as follows:

```toml
[project.entry-points."dataset_foundry.adapters"]
my_adapter = "my_package.adapter:discover"

[project.entry-points."dataset_foundry.processors"]
my_processor = "my_package.processor:process_record"
```

## 8. Progress, monitoring and review

`collect` reports source progress on stderr while keeping its final JSON on stdout; use
`--no-progress` for automation. `collection_summary.json`, `events.jsonl` and optional
`alerts.jsonl` distinguish completed/degraded sources and record quota, retry exhaustion,
rate-limit, challenge and CAPTCHA outcomes. Self-recovery is limited to crash resume and
bounded transient retry.

Run `dsforge review --run workspace/RUN`. The server binds only to `127.0.0.1`, generates a
session token, verifies each asset checksum before serving it, and stores
accepted/rejected/pending decisions in `review.sqlite3`. It never edits the manifests or
silently promotes candidates into a labeled dataset.

## 9. Operational limitations and assumptions

- Local directories are not filesystem snapshots; avoid changing source files during a
  run. Raw files already checkpointed are immutable and verified on resume.
- Resume requires an identical effective config fingerprint. It is recovery, not a way to
  add sources or change quotas in an existing run.
- A run directory is a trusted, single-writer workspace. Checkpoints are integrity-checked
  but not cryptographically signed, and concurrent `collect`/`process` writers are not
  supported.
- HTTP collection uses a bounded worker queue and per-host pacing. DNS is checked before requests but
  is not pinned to a resolved address; use controlled egress before accepting untrusted URL
  registries in higher-risk environments.
- MIME signatures are recorded for common media/archive/document types. `mime_validation`
  can be `off`, `warn` (default), or `strict`; source `allowed_mime_types` always fails closed
  for unknown/disallowed signatures. This is signature screening, not full media decoding.
- The workspace-level SQLite index validates referenced files before exact cross-run SHA-256
  matches. It does not perform perceptual or semantic deduplication.
- `max_source_mb` counts bytes of successfully collected candidates, including exact
  duplicates; rejected partial streams release their reservation.
- Browser rendering supports explicitly approved same-host pages, blocks service workers,
  redirects and cross-origin subresources, and has no login/CAPTCHA/stealth support.
- Alerts are local JSONL artifacts; external paging, dashboards and automatic operator
  notification are not included.
- Entry-point adapters use the fixed `Source` fields and existing downloader candidate kinds;
  arbitrary plugin-specific TOML or transports require an explicit core API change.
- Processing is sequential and recreates `derived/`. Keep the last successful export if a
  machine interruption occurs during expensive video processing.
- Hugging Face uses its official public API with no token. Gated/private/unavailable
  datasets are not bypassed.

## 10. AI-agent workflow

1. Read `AGENTS.md` then `docs/00-INDEX.md`.
2. Make small, isolated tasks: a new adapter, a processor, an export mapper, or integration tests.
3. Never change export fields without versioning the contract and tests.
4. Run `python -m unittest discover -s tests -v` before commit.
5. Test with synthetic fixtures; avoid checking real personal data into Git.

See `docs/AGENT-TASKS.md` for practical prompts and milestones.
