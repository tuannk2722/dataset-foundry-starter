import json
import tempfile
import unittest
from pathlib import Path
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
        cfg.write_text('''
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
''' + extra_sources, encoding="utf-8")
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
        extra = '''
[[sources]]
id = "uncleared"
adapter = "local_dir"
enabled = true
rights_confirmed = false
license = "unknown"
rights_note = "not cleared"
path = "data"
'''
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

    def test_corrupt_image_flagged_instead_of_crashing(self):
        (self.assets / "broken.png").write_bytes(b"this is not a PNG")
        run = self.root / "bad-image-run"
        collect(load_settings(self._config()), run)
        summary = process(run)
        self.assertGreaterEqual(summary["quality_flags"], 1)
        records = [json.loads(l) for l in (run / "processed.jsonl").read_text().splitlines()]
        row = next(r for r in records if r.get("original_name") == "broken.png")
        self.assertIn("invalid_image", row["quality_flags"])
