import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dataset_foundry.cli import build_parser, main


class CLITests(unittest.TestCase):
    def test_validate_loads_entry_point_plugins(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            config = root / "sources.toml"
            config.write_text(
                """
[[sources]]
id = "local"
adapter = "local_dir"
enabled = true
rights_confirmed = true
license = "self-created"
rights_note = "synthetic"
path = "data"
""",
                encoding="utf-8",
            )
            with (
                patch("dataset_foundry.cli.load_entry_point_plugins") as load_plugins,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                main(["validate", "--config", str(config)])
            load_plugins.assert_called_once_with()

    def test_process_accepts_repeated_plugin_processor_names(self):
        args = build_parser().parse_args(
            [
                "process",
                "--run",
                "run",
                "--processor",
                "first",
                "--processor",
                "second",
            ]
        )
        self.assertEqual(args.processors, ["first", "second"])


if __name__ == "__main__":
    unittest.main()
