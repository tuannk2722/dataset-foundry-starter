import json
import tempfile
import unittest
from pathlib import Path

from dataset_foundry.adapters.discovery import register_adapter, unregister_adapter
from dataset_foundry.config import load_settings
from dataset_foundry.models import Candidate
from dataset_foundry.pipeline import collect
from dataset_foundry.processors import (
    ProcessingContext,
    process,
    register_processor,
    unregister_processor,
)


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.asset = self.root / "synthetic.txt"
        self.asset.write_text("plugin-owned fixture", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_synthetic_adapter_and_processor_plugins(self):
        def discover_synthetic(source, _limits):
            yield Candidate(
                source_id=source.id,
                kind="local",
                origin="synthetic://fixture",
                name=self.asset.name,
                path=self.asset,
            )

        def mark_synthetic(record: dict, context: ProcessingContext):
            record["plugin_marker"] = f"processed-in-{context.run_dir.name}"
            return ()

        register_adapter("synthetic_fixture", discover_synthetic)
        register_processor("synthetic_marker", mark_synthetic)
        self.addCleanup(unregister_adapter, "synthetic_fixture")
        self.addCleanup(unregister_processor, "synthetic_marker")

        config = self.root / "sources.toml"
        config.write_text(
            """
[project]
name = "plugins"
[limits]
max_asset_mb = 1
max_assets_per_source = 2
max_zip_compression_ratio = 100
min_request_delay_seconds = 0.5
[[sources]]
id = "synthetic"
adapter = "synthetic_fixture"
enabled = true
rights_confirmed = true
license = "self-created"
rights_note = "synthetic test plugin"
""",
            encoding="utf-8",
        )
        run = self.root / "run"
        summary = collect(load_settings(config), run)
        self.assertEqual(summary["downloaded"], 1)
        process(run, processors=("synthetic_marker",))
        records = [
            json.loads(line)
            for line in (run / "processed.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(records[0]["plugin_marker"], "processed-in-run")

    def test_duplicate_plugin_registration_is_rejected(self):
        def plugin(_source, _limits):
            return iter(())

        register_adapter("one_time_plugin", plugin)
        self.addCleanup(unregister_adapter, "one_time_plugin")
        with self.assertRaisesRegex(ValueError, "already registered"):
            register_adapter("one_time_plugin", plugin)

    def test_processor_cannot_silently_promote_a_label(self):
        def unsafe_processor(record: dict, _context: ProcessingContext):
            record["label"] = "silently-promoted"
            return ()

        register_processor("unsafe_test_processor", unsafe_processor)
        self.addCleanup(unregister_processor, "unsafe_test_processor")
        config = self.root / "sources.toml"
        config.write_text(
            f"""
[project]
name = "unsafe-plugin"
[[sources]]
id = "local"
adapter = "local_dir"
enabled = true
rights_confirmed = true
license = "self-created"
rights_note = "synthetic"
path = "{self.root.as_posix()}"
""",
            encoding="utf-8",
        )
        run = self.root / "unsafe-run"
        collect(load_settings(config), run)
        with self.assertRaisesRegex(ValueError, "protected field 'label'"):
            process(run, processors=("unsafe_test_processor",))
        self.assertFalse((run / "processed.jsonl").exists())
