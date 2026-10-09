import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from dataset_foundry.adapters.discovery import discover
from dataset_foundry.models import Limits, Settings, Source
from dataset_foundry.pipeline import collect


class ArchiveTests(unittest.TestCase):
    def test_archive_skips_traversal_and_oversize(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "sample.zip"
            with ZipFile(archive, "w") as zf:
                zf.writestr("../bad.jpg", b"bad")
                zf.writestr("C:/bad.jpg", b"bad")
                zf.writestr("data/good.jpg", b"good")
                zf.writestr("data/large.jpg", b"x" * 1_050_000)
            src = Source("zip", "local_zip", True, True, "test", "owned", path=archive)
            got = list(discover(src, Limits(max_asset_mb=1, max_assets_per_source=10)))
            self.assertEqual(
                [x.member for x in got],
                ["../bad.jpg", "C:/bad.jpg", "data/good.jpg", "data/large.jpg"],
            )
            self.assertEqual(got[0].meta["skip_reason"], "unsafe archive member path")
            self.assertEqual(got[1].meta["skip_reason"], "unsafe archive member path")
            self.assertNotIn("skip_reason", got[2].meta)
            self.assertIn("size limit", got[3].meta["skip_reason"])

            run = Path(temp) / "run"
            settings = Settings(
                "archive-audit",
                Limits(max_asset_mb=1, max_assets_per_source=10),
                (src,),
                Path(temp) / "config.toml",
            )
            summary = collect(settings, run)
            self.assertEqual(summary["downloaded"], 1)
            self.assertEqual(summary["skipped"], 3)
            manifest = (run / "manifest.jsonl").read_text(encoding="utf-8")
            self.assertIn("unsafe archive member path", manifest)
            self.assertIn("size limit", manifest)
