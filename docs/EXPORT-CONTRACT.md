# Candidate Batch Export Contract v1

## Output layout

```text
BATCH.zip
  schema.json
  manifest.jsonl
  sources.json
  collection_summary.json
  processing_summary.json  # if processed
  events.jsonl
  README.txt
  assets/raw/SOURCE/ASSET.jpg
  assets/derived/PARENT/frame_00001.jpg  # optional
```

`schema.json` declares `schema_version: candidate-batch-v1`, `kind: unreviewed_candidates`, `labels_verified: false`, `split_assigned: false`.

Before export, Dataset Foundry re-hashes every included file and rejects missing, unsafe,
size-mismatched or checksum-mismatched assets. The ZIP is written to a sibling temporary
file and atomically replaces the requested output only after successful completion.
Manifest rows with an unknown status or promoted review/label/split state are rejected.

## Required per-asset fields

`asset_id`: stable per source+candidate origin; not a global universal asset ID. `parent_id`: null for original / original asset ID for derived. `source_id`: registry ID. `source_adapter`: source type. `origin`: original path/URL/repository location (may contain sensitive local paths; audit before sharing). `original_name`: basename only. `license`, `rights_note`, `collected_at`. `mime_guess`, `media_kind`. `status`: downloaded | derived | duplicate | skipped | error. `review_status`: unreviewed. `label`: null. `split`: null. `file_path`: run-relative path for exported assets (in ZIP prepend `assets/`). `sha256`, `bytes`: included for successfully retrieved files. Optional: `duplicate_of`, `reason`, `width`, `height`, `ahash64`, `quality_flags`.

## Intended importer flow

1. Verify ZIP integrity, safe member paths, checksums, rights records and contract version.
2. Extract into a **quarantine/staging directory**, not train/val/test.
3. Validate media decoding, metadata and provenance; inspect parent/child associations.
4. Run cross-source/cross-run exact and perceptual duplicate checks; group frames/subjects/sessions.
5. Map task-specific ontology in a separately versioned mapping/review policy; verify proposed labels with human review.
6. Promote accepted samples to a versioned training candidate pool. Preserve frozen evaluation datasets and domain boundaries.

**Important:** This generic package does not provide bbox detection, person tracking, phone_use or looking_around labels, and does not replace the project's current audit pipeline.
