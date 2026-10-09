import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dataset_foundry.manifest import write_json


class ManifestTests(unittest.TestCase):
    def test_failed_atomic_replace_preserves_previous_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            write_json(path, {"version": 1})
            original = path.read_bytes()
            with (
                patch("dataset_foundry.manifest.os.replace", side_effect=OSError("disk error")),
                self.assertRaisesRegex(OSError, "disk error"),
            ):
                write_json(path, {"version": 2})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])
