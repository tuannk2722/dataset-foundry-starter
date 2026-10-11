import unittest
from unittest.mock import patch

from dataset_foundry.adapters.discovery import adapter_names, unregister_adapter
from dataset_foundry.plugins import (
    _LOADED_ENTRY_POINTS,
    ADAPTER_ENTRY_POINT_GROUP,
    PROCESSOR_ENTRY_POINT_GROUP,
    load_entry_point_plugins,
)
from dataset_foundry.processors import processor_names, unregister_processor


class _Distribution:
    def __init__(self, name):
        self.name = name


class _EntryPoint:
    def __init__(self, group, name, value, plugin=None, error=None, load_log=None):
        self.group = group
        self.name = name
        self.value = value
        self.dist = _Distribution("synthetic-plugin")
        self.plugin = plugin
        self.error = error
        self.load_log = load_log

    def load(self):
        if self.load_log is not None:
            self.load_log.append(self.name)
        if self.error is not None:
            raise self.error
        return self.plugin


class EntryPointPluginTests(unittest.TestCase):
    ADAPTER_NAMES = ("ep_adapter_a", "ep_adapter_b", "ep_adapter_good")
    PROCESSOR_NAMES = ("ep_processor_bad", "ep_processor_z")

    def tearDown(self):
        for name in self.ADAPTER_NAMES:
            if name in adapter_names():
                unregister_adapter(name)
        for name in self.PROCESSOR_NAMES:
            if name in processor_names():
                unregister_processor(name)
        _LOADED_ENTRY_POINTS.clear()

    @staticmethod
    def _entry_points(entries):
        def select(*, group):
            return [entry for entry in entries if entry.group == group]

        return select

    def test_loads_in_deterministic_order_and_is_idempotent(self):
        load_log = []

        def adapter_a(_source, _limits):
            return iter(())

        def adapter_b(_source, _limits):
            return iter(())

        def processor_z(_record, _context):
            return ()

        entries = [
            _EntryPoint(
                ADAPTER_ENTRY_POINT_GROUP,
                "ep_adapter_b",
                "example:b",
                adapter_b,
                load_log=load_log,
            ),
            _EntryPoint(
                PROCESSOR_ENTRY_POINT_GROUP,
                "ep_processor_z",
                "example:z",
                processor_z,
                load_log=load_log,
            ),
            _EntryPoint(
                ADAPTER_ENTRY_POINT_GROUP,
                "ep_adapter_a",
                "example:a",
                adapter_a,
                load_log=load_log,
            ),
        ]
        with patch(
            "dataset_foundry.plugins.metadata.entry_points",
            side_effect=self._entry_points(entries),
        ):
            first = load_entry_point_plugins()
            second = load_entry_point_plugins()

        self.assertEqual(load_log, ["ep_adapter_a", "ep_adapter_b", "ep_processor_z"])
        self.assertEqual(first, second)
        self.assertEqual(
            first,
            {
                ADAPTER_ENTRY_POINT_GROUP: ("ep_adapter_a", "ep_adapter_b"),
                PROCESSOR_ENTRY_POINT_GROUP: ("ep_processor_z",),
            },
        )
        self.assertIn("ep_adapter_a", adapter_names())
        self.assertIn("ep_adapter_b", adapter_names())
        self.assertIn("ep_processor_z", processor_names())

    def test_non_callable_fails_without_partial_registration(self):
        def good_adapter(_source, _limits):
            return iter(())

        entries = [
            _EntryPoint(
                ADAPTER_ENTRY_POINT_GROUP,
                "ep_adapter_good",
                "example:good",
                good_adapter,
            ),
            _EntryPoint(
                PROCESSOR_ENTRY_POINT_GROUP,
                "ep_processor_bad",
                "example:bad",
                object(),
            ),
        ]
        with patch(
            "dataset_foundry.plugins.metadata.entry_points",
            side_effect=self._entry_points(entries),
        ), self.assertRaisesRegex(TypeError, "must resolve to a callable"):
            load_entry_point_plugins()

        self.assertNotIn("ep_adapter_good", adapter_names())
        self.assertNotIn("ep_processor_bad", processor_names())

    def test_load_error_fails_without_partial_registration(self):
        def adapter_a(_source, _limits):
            return iter(())

        entries = [
            _EntryPoint(
                ADAPTER_ENTRY_POINT_GROUP,
                "ep_adapter_a",
                "example:a",
                adapter_a,
            ),
            _EntryPoint(
                ADAPTER_ENTRY_POINT_GROUP,
                "ep_adapter_b",
                "example:b",
                error=ImportError("missing optional dependency"),
            ),
        ]
        with patch(
            "dataset_foundry.plugins.metadata.entry_points",
            side_effect=self._entry_points(entries),
        ), self.assertRaisesRegex(RuntimeError, "ep_adapter_b"):
            load_entry_point_plugins()

        self.assertNotIn("ep_adapter_a", adapter_names())
        self.assertNotIn("ep_adapter_b", adapter_names())

    def test_builtin_collision_is_rejected_before_loading_plugin_code(self):
        load_log = []
        entries = [
            _EntryPoint(
                ADAPTER_ENTRY_POINT_GROUP,
                "local_dir",
                "example:collision",
                lambda _source, _limits: iter(()),
                load_log=load_log,
            )
        ]
        with patch(
            "dataset_foundry.plugins.metadata.entry_points",
            side_effect=self._entry_points(entries),
        ), self.assertRaisesRegex(ValueError, "collides with an existing registration"):
            load_entry_point_plugins()

        self.assertEqual(load_log, [])

    def test_duplicate_names_are_rejected_before_loading_plugin_code(self):
        load_log = []
        entries = [
            _EntryPoint(
                ADAPTER_ENTRY_POINT_GROUP,
                "ep_adapter_a",
                "example:first",
                lambda _source, _limits: iter(()),
                load_log=load_log,
            ),
            _EntryPoint(
                ADAPTER_ENTRY_POINT_GROUP,
                "ep_adapter_a",
                "example:second",
                lambda _source, _limits: iter(()),
                load_log=load_log,
            ),
        ]
        with patch(
            "dataset_foundry.plugins.metadata.entry_points",
            side_effect=self._entry_points(entries),
        ), self.assertRaisesRegex(ValueError, "Duplicate plugin entry point"):
            load_entry_point_plugins()

        self.assertEqual(load_log, [])


if __name__ == "__main__":
    unittest.main()
