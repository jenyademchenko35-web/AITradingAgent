"""Exercise the agent's actual stats functions without optional trading imports."""

import ast
import errno
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


AGENT_FILE = Path(__file__).resolve().parents[1] / "multi_timeframe_agent_v3.py"


class AgentStatsAtomicTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "agent_v3_stats.json"
        definitions = ast.parse(AGENT_FILE.read_text(encoding="utf-8")).body
        functions = [node for node in definitions if isinstance(node, ast.FunctionDef)
                     and node.name in {"save_stats", "load_stats"}]
        self.assertEqual({node.name for node in functions}, {"save_stats", "load_stats"})
        self.writes = []
        self.diagnostics = []
        self.namespace = {
            "os": os, "json": json, "stat": stat,
            "tempfile": tempfile, "STATS_FILE": str(self.path),
            "LOGGER": SimpleNamespace(
                csv_read=lambda path: None,
                csv_write=lambda path: self.writes.append(path),
                timestamped=lambda message: self.diagnostics.append(json.loads(message)),
            ),
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(AGENT_FILE), "exec"),
             self.namespace)

    def _old_stats(self):
        old = {"runs": 7, "analyzed_symbols": 14, "api_errors": 1,
               "setup_signals": 3, "no_trade_signals": 4}
        self.path.write_text(json.dumps(old, indent=2), encoding="utf-8")
        return old, self.path.read_bytes()

    def test_new_save_and_normal_load_preserve_counters(self):
        expected = {"runs": 8, "analyzed_symbols": 17, "api_errors": 1,
                    "setup_signals": 4, "no_trade_signals": 5}
        self.namespace["save_stats"](expected)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), expected)
        self.assertEqual(self.namespace["load_stats"](), expected)
        self.assertEqual(self.writes, [str(self.path)])

    def test_temp_fsync_failure_preserves_existing_file(self):
        _, previous = self._old_stats()
        with patch.object(os, "fsync", side_effect=OSError("sync failed")):
            with self.assertRaisesRegex(OSError, "sync failed"):
                self.namespace["save_stats"]({"runs": 99})
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.glob(".agent_v3_stats.json.*.tmp")), [])
        self.assertEqual(self.writes, [])

    def test_replace_failure_preserves_existing_file(self):
        _, previous = self._old_stats()
        with patch.object(os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                self.namespace["save_stats"]({"runs": 99})
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.glob(".agent_v3_stats.json.*.tmp")), [])
        self.assertEqual(self.writes, [])

    def test_replacement_contains_complete_new_payload(self):
        old, previous = self._old_stats()
        expected = {**old, "runs": 8, "analyzed_symbols": 19}
        self.namespace["save_stats"](expected)
        self.assertNotEqual(self.path.read_bytes(), previous)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), expected)
        self.assertEqual(self.namespace["load_stats"](), expected)
        self.assertEqual(list(self.path.parent.glob(".agent_v3_stats.json.*.tmp")), [])

    @unittest.skipUnless(os.name == "posix", "directory fsync is POSIX-only")
    def test_directory_fsync_failure_after_replace_is_nonfatal(self):
        old, _ = self._old_stats()
        expected = {**old, "runs": 8}
        real_fsync = os.fsync
        calls = 0

        def fail_directory_fsync(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(errno.EIO, "directory sync failed")
            return real_fsync(descriptor)

        with patch.object(os, "fsync", side_effect=fail_directory_fsync):
            self.namespace["save_stats"](expected)

        self.assertEqual(calls, 2)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), expected)
        self.assertEqual(self.writes, [str(self.path)])
        self.assertEqual(len(self.diagnostics), 1)
        self.assertEqual(self.diagnostics[0]["event"], "agent_stats_directory_fsync_warning")
        self.assertIn("directory sync failed", self.diagnostics[0]["error"])
        self.assertEqual(list(self.path.parent.glob(".agent_v3_stats.json.*.tmp")), [])

    def test_load_stats_existing_and_missing_file(self):
        expected, _ = self._old_stats()
        self.assertEqual(self.namespace["load_stats"](), expected)
        self.path.unlink()
        self.assertEqual(self.namespace["load_stats"](), {
            "runs": 0, "analyzed_symbols": 0, "api_errors": 0,
            "setup_signals": 0, "no_trade_signals": 0,
        })


if __name__ == "__main__":
    unittest.main()
