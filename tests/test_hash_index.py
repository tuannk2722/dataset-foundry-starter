import hashlib
import tempfile
import unittest
from pathlib import Path

from dataset_foundry.hash_index import HashIndex


class HashIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "index" / "hashes.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def _asset(self, name: str, content: bytes) -> tuple[Path, str]:
        path = self.root / name
        path.write_bytes(content)
        return path, hashlib.sha256(content).hexdigest()

    def test_register_and_lookup_persist_across_contexts(self):
        asset, digest = self._asset("asset.bin", b"candidate bytes")
        with HashIndex(self.database) as index:
            registered = index.register(
                digest, asset.stat().st_size, asset, run_id="run-1", asset_id="asset-1"
            )
            self.assertEqual(registered.file_path, asset.resolve())
            self.assertEqual(index.lookup(digest), registered)

        with HashIndex(self.database) as reopened:
            found = reopened.lookup(digest, expected_bytes=asset.stat().st_size)
            self.assertIsNotNone(found)
            self.assertEqual(found.run_id, "run-1")

    def test_valid_first_occurrence_is_preserved(self):
        first, digest = self._asset("first.bin", b"same")
        second, _ = self._asset("second.bin", b"same")
        with HashIndex(self.database) as index:
            original = index.register(digest, 4, first, run_id="run-1", asset_id="first")
            duplicate = index.register(digest, 4, second, run_id="run-2", asset_id="second")
            self.assertEqual(duplicate, original)
            self.assertEqual(duplicate.file_path, first.resolve())

    def test_lookup_removes_missing_or_tampered_target(self):
        asset, digest = self._asset("asset.bin", b"original")
        with HashIndex(self.database) as index:
            index.register(digest, 8, asset, run_id="run-1", asset_id="asset-1")
            asset.write_bytes(b"tampered")  # same length, so checksum validation is required
            self.assertIsNone(index.lookup(digest))

            replacement, _ = self._asset("replacement.bin", b"original")
            record = index.register(
                digest, 8, replacement, run_id="run-2", asset_id="asset-2"
            )
            self.assertEqual(record.file_path, replacement.resolve())
            replacement.unlink()
            self.assertIsNone(index.lookup(digest))

    def test_rejects_invalid_metadata_and_mismatched_file(self):
        asset, digest = self._asset("asset.bin", b"content")
        with HashIndex(self.database) as index:
            with self.assertRaises(ValueError):
                index.lookup(digest.upper())
            with self.assertRaises(ValueError):
                index.register(digest, 999, asset, run_id="run", asset_id="asset")
            with self.assertRaises(FileNotFoundError):
                index.register(
                    digest,
                    asset.stat().st_size,
                    self.root / "missing.bin",
                    run_id="run",
                    asset_id="asset",
                )

    def test_close_is_idempotent_and_operations_fail_clearly(self):
        index = HashIndex(self.database)
        index.close()
        index.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            index.lookup("0" * 64)


if __name__ == "__main__":
    unittest.main()
