import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dataset_foundry.config import load_settings
from dataset_foundry.pipeline import collect


class ReliabilityFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets = self.root / "data"
        self.assets.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def _config(
        self, *, max_source_mb: int = 4, mime_validation: str = "warn", source: str = ""
    ) -> Path:
        config = self.root / "sources.toml"
        config.write_text(
            f"""
[project]
name = "reliability"
[limits]
max_asset_mb = 1
max_source_mb = {max_source_mb}
max_assets_per_source = 20
max_zip_compression_ratio = 100
min_request_delay_seconds = 0.5
max_workers = 3
retry_attempts = 3
retry_backoff_seconds = 0
mime_validation = "{mime_validation}"
[[sources]]
id = "local"
adapter = "local_dir"
enabled = true
rights_confirmed = true
license = "self-created"
rights_note = "synthetic"
path = "data"
{source}
""",
            encoding="utf-8",
        )
        return config

    def _rows(self, run: Path) -> list[dict]:
        return [json.loads(line) for line in (run / "manifest.jsonl").read_text().splitlines()]

    def test_source_byte_quota_and_progress(self):
        (self.assets / "one.bin").write_bytes(b"a" * 700_000)
        (self.assets / "two.bin").write_bytes(b"b" * 700_000)
        config = self._config(max_source_mb=1)
        events = []
        run = self.root / "work" / "quota"
        summary = collect(load_settings(config), run, progress=events.append)

        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertLessEqual(summary["collected_bytes"], 1024 * 1024)
        self.assertTrue(any(event.get("phase") == "asset" for event in events))
        self.assertEqual(events[-1]["phase"], "collect")
        alert_text = (run / "alerts.jsonl").read_text()
        self.assertIn("source exceeds maximum bytes", alert_text)
        self.assertIn('"category": "source_quota_exceeded"', alert_text)

    def test_cross_run_hash_index_marks_second_run_duplicates(self):
        (self.assets / "one.txt").write_text("first", encoding="utf-8")
        (self.assets / "two.txt").write_text("second", encoding="utf-8")
        settings = load_settings(self._config())
        run_one = self.root / "work" / "one"
        run_two = self.root / "work" / "two"
        self.assertEqual(collect(settings, run_one)["downloaded"], 2)
        second = collect(settings, run_two)

        self.assertEqual(second["downloaded"], 0)
        self.assertEqual(second["duplicate"], 2)
        self.assertTrue(
            all(row.get("duplicate_scope") == "cross_run" for row in self._rows(run_two))
        )

    def test_strict_mime_mismatch_is_skipped_and_not_retained(self):
        (self.assets / "spoofed.png").write_text("not an image", encoding="utf-8")
        config = self._config(mime_validation="strict")
        run = self.root / "work" / "mime"
        summary = collect(load_settings(config), run)

        self.assertEqual(summary["skipped"], 1)
        row = self._rows(run)[0]
        self.assertIn("content signature mismatch", row["reason"])
        self.assertFalse(any((run / "raw").rglob("*.png")))

    def test_bounded_workers_fetch_concurrently(self):
        for index in range(6):
            (self.assets / f"{index}.txt").write_text(str(index), encoding="utf-8")
        settings = load_settings(self._config())
        run = self.root / "work" / "parallel"
        active = 0
        maximum = 0
        lock = threading.Lock()
        from dataset_foundry import pipeline

        original = pipeline._fetch_candidate

        def observed(*args, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.03)
                return original(*args, **kwargs)
            finally:
                with lock:
                    active -= 1

        with patch("dataset_foundry.pipeline._fetch_candidate", side_effect=observed):
            collect(settings, run)
        self.assertGreaterEqual(maximum, 2)
        self.assertLessEqual(maximum, settings.limits.max_workers)


if __name__ == "__main__":
    unittest.main()
