# AI Agent Instructions — Dataset Foundry Starter

## Mission
Create and evolve an auditable, reusable **multi-source, multi-modal candidate-ingestion** tool. It must NOT be tied to phone detection, cheating detection, or any one model/class ontology.

## Mandatory first reads
1. `README.md` (quickstart / current capabilities)
2. `docs/00-INDEX.md` (read only topic-relevant docs next)
3. The smallest relevant source module and tests.

## Non-negotiable invariants
- No access-control, CAPTCHA, robots.txt, throttling, login or licensing bypass. Skip blocked sources and preserve reasons. Don't introduce stealth/proxy rotation.
- No scraping unknown sites without specific authorization. Respect privacy; don't collect unconsented personal information.
- No silent auto-label promotion: collected/derived assets remain unreviewed until a downstream validated human-review workflow accepts them.
- Preserve immutable raw assets, provenance, source/group/parent IDs, original license notes and content SHA-256. Do not split train/val/test here.
- Bounds: downloaded bytes/file count, video frame count, ZIP decompression ratio, request pace, timeouts. Prefer streaming to RAM-heavy aggregation.
- No silent manifest schema changes. If breaking: bump contract schema version and adapt downstream importers.
- Keep `src/dataset_foundry/adapters/` focused on data access; `processors.py` focuses on generic transformations; `exporter.py` never performs labeling.
- Never commit real datasets, workspace/, exports/, tokens, PII, or heavyweight cache.

## Definition of Done
- One feature per task, explicit changed-file summary and limitations.
- Tests for happy path, failure/skip path, unsafe inputs, idempotence if applicable.
- `python -m unittest discover -s tests -v` passes; run formatting/lint if installed.
- Update `docs/00-INDEX.md` only if new docs added.
- Write assumptions and source-specific limitations. Don't fabricate successful crawl runs.

## Parallel subagents (when appropriate)
Independent adapters, generic processors and docs/tests may run separately. Do NOT concurrently edit `models.py`, `config.py`, `manifest.py`, `exporter.py` or export schema without clear ownership. Lead agent integrates and runs tests.

## Recommended file boundaries
- Add source adapter: new `adapters/<adapter>.py`, register in `adapters/discovery.py`, adjust `config.py` and `downloader.py` only as needed; adapter tests + docs.
- Add processing: `processors.py` (or new registered processor module) + tests.
- Add destination: new exporter/integration adapter; preserve existing candidate-batch-v1.
