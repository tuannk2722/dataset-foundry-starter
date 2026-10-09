import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from dataset_foundry.adapters.discovery import discover
from dataset_foundry.models import Limits, Source


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
            self.assertEqual([x.member for x in got], ["data/good.jpg"])
