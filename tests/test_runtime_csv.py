"""Golden tests for low-overhead runtime CSV writes."""

from __future__ import annotations

import csv
import multiprocessing
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import runtime_csv
from runtime_csv import (
    CSVSchemaMigrationError,
    CSVSchemaMismatchError,
    append_row_atomic_or_locked,
    ensure_header,
    get_runtime_csv_stats,
    read_header,
    reset_runtime_csv_state,
)


def hold_interprocess_lock(path: str, ready, release) -> None:
    """Hold the runtime lock in a child process for serialization testing."""
    with runtime_csv._interprocess_lock(path):
        ready.set()
        release.wait(5)


class RuntimeCSVTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_runtime_csv_state()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_first_check_reads_header_and_second_check_hits_cache(self) -> None:
        path = self.root / "signals.csv"
        path.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")

        first = ensure_header(path, ["a", "b"])
        first_stats = get_runtime_csv_stats()
        second = ensure_header(path, ["a", "b"])
        second_stats = get_runtime_csv_stats()

        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(first_stats["schema_checks"], 1)
        self.assertEqual(second_stats["schema_cache_hits"], 1)
        self.assertEqual(
            first_stats["bytes_read_for_schema"],
            len("a,b\n".encode("utf-8")),
        )
        self.assertEqual(
            second_stats["bytes_read_for_schema"],
            first_stats["bytes_read_for_schema"],
        )
        self.assertGreaterEqual(
            second_stats["bytes_saved_estimate"],
            path.stat().st_size,
        )

    def test_append_refreshes_cache_without_full_file_read(self) -> None:
        path = self.root / "runtime.csv"
        append_row_atomic_or_locked(path, ["a", "b"], {"a": 1, "b": 2})
        before = get_runtime_csv_stats()["bytes_read_for_schema"]
        append_row_atomic_or_locked(path, ["a", "b"], {"a": 3, "b": 4})
        after = get_runtime_csv_stats()["bytes_read_for_schema"]

        self.assertEqual(before, after)
        with path.open("r", newline="", encoding="utf-8") as file:
            self.assertEqual(list(csv.reader(file)), [["a", "b"], ["1", "2"], ["3", "4"]])

    def test_external_header_change_forces_recheck(self) -> None:
        path = self.root / "changed.csv"
        path.write_text("a,b\n1,2\n", encoding="utf-8")
        ensure_header(path, ["a", "b"])
        time.sleep(0.002)
        path.write_text("a,c\n1,2\n", encoding="utf-8")

        with self.assertRaises(CSVSchemaMismatchError):
            ensure_header(path, ["a", "b"], allow_migration=False)

        self.assertEqual(get_runtime_csv_stats()["schema_checks"], 2)

    def test_additive_migration_keeps_rows_and_creates_backup(self) -> None:
        path = self.root / "migration.csv"
        path.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")

        result = ensure_header(path, ["a", "quality", "b"])

        self.assertTrue(result.migrated)
        self.assertEqual(result.rows_migrated, 2)
        self.assertTrue(Path(result.backup_path).exists())
        self.assertEqual(read_header(path, track_metrics=False), ["a", "quality", "b"])
        with path.open("r", newline="", encoding="utf-8") as file:
            rows = list(csv.reader(file))
        self.assertEqual(rows[1:], [["1", "", "2"], ["3", "", "4"]])

    def test_migration_rejects_column_removal_without_changing_data(self) -> None:
        path = self.root / "removal.csv"
        original = "a,b,c\n1,2,3\n"
        path.write_text(original, encoding="utf-8")

        with self.assertRaises(CSVSchemaMigrationError):
            ensure_header(path, ["a", "b"])

        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_migration_rejects_unmappable_row_without_changing_data(self) -> None:
        path = self.root / "malformed.csv"
        original = "a,b\n1,2,extra\n"
        path.write_text(original, encoding="utf-8")

        with self.assertRaises(CSVSchemaMigrationError):
            ensure_header(path, ["a", "quality", "b"])

        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_successful_append_is_fsynced(self) -> None:
        path = self.root / "durable.csv"
        path.write_text("a,b\n", encoding="utf-8")
        with patch("runtime_csv.os.fsync") as fsync:
            append_row_atomic_or_locked(path, ["a", "b"], ["1", "2"])
        fsync.assert_called_once()

    @unittest.skipIf(runtime_csv.fcntl is None, "fcntl unavailable")
    def test_interprocess_lock_serializes_append(self) -> None:
        path = self.root / "locked.csv"
        path.write_text("a,b\n", encoding="utf-8")
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()
        child = context.Process(
            target=hold_interprocess_lock,
            args=(str(path), ready, release),
        )
        child.start()
        self.assertTrue(ready.wait(2))

        finished = threading.Event()

        def append() -> None:
            append_row_atomic_or_locked(path, ["a", "b"], ["1", "2"])
            finished.set()

        thread = threading.Thread(target=append)
        thread.start()
        time.sleep(0.05)
        self.assertFalse(finished.is_set())
        release.set()
        child.join(2)
        thread.join(2)

        self.assertEqual(child.exitcode, 0)
        self.assertTrue(finished.is_set())
        self.assertIn("1,2", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
