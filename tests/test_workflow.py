import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from dataset_foundry.config import load_settings
from dataset_foundry.exporter import export_batch
from dataset_foundry.pipeline import collect
from dataset_foundry.processors import process


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets = self.root / "data"
        self.assets.mkdir()
        (self.assets / "one.txt").write_text("same bytes", encoding="utf-8")
        (self.assets / "copy.txt").write_text("same bytes", encoding="utf-8")
        (self.assets / "different.csv").write_text("x,y\n1,2\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def _config(self, extra_sources=""):
        cfg = self.root / "sources.toml"
        cfg.write_text(
            """
[project]
name = "test"
[limits]
max_asset_mb = 1
max_assets_per_source = 10
max_zip_compression_ratio = 100
min_request_delay_seconds = 0.5
[[sources]]
id = "local"
adapter = "local_dir"
enabled = true
rights_confirmed = true
license = "self-created"
rights_note = "synthetic"
path = "data"
"""
            + extra_sources,
            encoding="utf-8",
        )
        return cfg

    def test_offline_end_to_end(self):
        cfg = load_settings(self._config())
        run = self.root / "work" / "run1"
        summary = collect(cfg, run)
        self.assertEqual(summary["downloaded"], 2)
        self.assertEqual(summary["duplicate"], 1)
        proc = process(run)
        self.assertEqual(proc["processed_assets"], 2)
        target = self.root / "output.zip"
        exported = export_batch(run, target)
        self.assertEqual(exported["exported_assets"], 2)
        with ZipFile(target) as zipfile:
            names = zipfile.namelist()
            self.assertEqual(len([x for x in names if x.startswith("assets/")]), 2)
            schema = json.loads(zipfile.read("schema.json"))
            self.assertEqual(schema["schema_version"], "candidate-batch-v1")
            rows = [json.loads(x) for x in zipfile.read("manifest.jsonl").splitlines()]
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(x["review_status"] == "unreviewed" for x in rows))
            self.assertTrue(all(x["split"] is None for x in rows))
        with self.assertRaises(FileExistsError):
            collect(cfg, run)

    def test_rights_unconfirmed_skips(self):
        extra = """
[[sources]]
id = "uncleared"
adapter = "local_dir"
enabled = true
rights_confirmed = false
license = "unknown"
rights_note = "not cleared"
path = "data"
"""
        run = self.root / "run"
        collect(load_settings(self._config(extra)), run)
        events = (run / "events.jsonl").read_text()
        self.assertIn("rights_not_confirmed", events)
        self.assertNotIn('"source_id": "uncleared", "status": "started"', events)

    def test_reject_bad_ids(self):
        cfg = self._config().read_text().replace('id = "local"', 'id = "../escape"')
        bad = self.root / "bad.toml"
        bad.write_text(cfg)
        with self.assertRaises(ValueError):
            load_settings(bad)

    def test_fail_closed_on_string_boolean_and_unknown_license(self):
        quoted = (
            self._config()
            .read_text(encoding="utf-8")
            .replace("rights_confirmed = true", 'rights_confirmed = "false"')
        )
        bad_boolean = self.root / "bad-boolean.toml"
        bad_boolean.write_text(quoted, encoding="utf-8")
        with self.assertRaisesRegex(TypeError, "TOML boolean"):
            load_settings(bad_boolean)

        unknown = (
            self._config()
            .read_text(encoding="utf-8")
            .replace('license = "self-created"', 'license = "unknown"')
        )
        bad_license = self.root / "bad-license.toml"
        bad_license.write_text(unknown, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "known license"):
            load_settings(bad_license)

    def test_corrupt_image_flagged_instead_of_crashing(self):
        (self.assets / "broken.png").write_bytes(b"this is not a PNG")
        run = self.root / "bad-image-run"
        collect(load_settings(self._config()), run)
        summary = process(run)
        self.assertGreaterEqual(summary["quality_flags"], 1)
        records = [json.loads(l) for l in (run / "processed.jsonl").read_text().splitlines()]
        row = next(r for r in records if r.get("original_name") == "broken.png")
        self.assertIn("invalid_image", row["quality_flags"])

    def test_resume_recovers_raw_asset_written_before_checkpoint(self):
        cfg = load_settings(self._config())
        run = self.root / "interrupted"
        with (
            patch(
                "dataset_foundry.pipeline._write_checkpoint",
                side_effect=KeyboardInterrupt("simulated power loss"),
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            collect(cfg, run)

        orphan = next((run / "raw" / "local").iterdir())
        orphan_mtime = orphan.stat().st_mtime_ns
        self.assertEqual((run / "manifest.jsonl").read_text(encoding="utf-8"), "")
        (run / "processed.jsonl").write_text("{}\n", encoding="utf-8")
        (run / "processing_summary.json").write_text("{}\n", encoding="utf-8")
        (run / "derived").mkdir()
        (run / "derived" / "stale.txt").write_text("stale", encoding="utf-8")

        summary = collect(cfg, run, resume=True)
        self.assertEqual(summary["downloaded"], 2)
        self.assertEqual(summary["duplicate"], 1)
        self.assertEqual(orphan.stat().st_mtime_ns, orphan_mtime)
        self.assertEqual(len(list((run / "checkpoints").glob("*.json"))), 3)
        self.assertFalse((run / "processed.jsonl").exists())
        self.assertFalse((run / "processing_summary.json").exists())
        self.assertFalse((run / "derived").exists())

        process(run)
        rows_before = (run / "manifest.jsonl").read_bytes()
        again = collect(cfg, run, resume=True)
        self.assertEqual(again["downloaded"], 2)
        self.assertEqual((run / "manifest.jsonl").read_bytes(), rows_before)
        self.assertTrue((run / "processed.jsonl").exists())

    def test_resume_rejects_changed_configuration(self):
        config_path = self._config()
        cfg = load_settings(config_path)
        run = self.root / "completed"
        collect(cfg, run)
        changed = config_path.read_text(encoding="utf-8").replace(
            "max_assets_per_source = 10", "max_assets_per_source = 9"
        )
        config_path.write_text(changed, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not match"):
            collect(load_settings(config_path), run, resume=True)

    def test_export_rejects_modified_raw_asset(self):
        run = self.root / "tampered-run"
        collect(load_settings(self._config()), run)
        record = next(
            json.loads(line)
            for line in (run / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
            if json.loads(line)["status"] == "downloaded"
        )
        (run / record["file_path"]).write_text("tampered", encoding="utf-8")
        output = self.root / "tampered.zip"
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            export_batch(run, output)
        self.assertFalse(output.exists())

    def test_export_rejects_silent_label_promotion(self):
        run = self.root / "promoted-run"
        collect(load_settings(self._config()), run)
        process(run)
        records = [
            json.loads(line)
            for line in (run / "processed.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        records[0]["review_status"] = "accepted"
        records[0]["label"] = "unaudited"
        (run / "processed.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        output = self.root / "promoted.zip"
        with self.assertRaisesRegex(ValueError, "promoted review data"):
            export_batch(run, output)
        self.assertFalse(output.exists())

    def test_export_with_review_filter(self):
        run = self.root / "filter-run"
        collect(load_settings(self._config()), run)
        process(run)
        from dataset_foundry.review import ReviewStore
        store = ReviewStore(run)
        candidates = store.candidates()
        first_id = candidates[0]["asset_id"]
        store.set_decision(first_id, "accepted", "good sample")
        output = self.root / "filtered.zip"
        exported = export_batch(run, output, review_filter="accepted")
        self.assertEqual(exported["exported_assets"], 1)
        with ZipFile(output) as zipfile:
            names = zipfile.namelist()
            self.assertEqual(len([x for x in names if x.startswith("assets/")]), 1)
            schema = json.loads(zipfile.read("schema.json"))
            self.assertEqual(schema["review_filter"], "accepted")
            self.assertIn("review_decisions.json", names)

