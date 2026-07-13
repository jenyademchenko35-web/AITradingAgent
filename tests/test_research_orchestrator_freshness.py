"""Freshness and metric-unit gate regression tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from research_orchestrator.artifact_registry import ArtifactRegistry
from research_orchestrator.models import ArtifactSpec
from tests.orchestrator_helpers import write_report


class OrchestratorFreshnessTest(TestCase):
    def test_old_report_is_excluded(self) -> None:
        now = datetime.now(timezone.utc)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_report(
                root,
                "old.json",
                generated_at=(now - timedelta(hours=25)).isoformat(),
            )
            spec = ArtifactSpec(
                "old", "METRICS", "old.json", False,
                "1.0", ("R",), 24, "fixture.py",
            )
            record = ArtifactRegistry(
                root, specs=(spec,), now=now
            ).load_all()["old"]

            self.assertEqual(record.status, "STALE")
            self.assertTrue(any("TTL" in reason for reason in record.reasons))

    def test_different_metric_units_are_not_mixed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_report(root, "percent.json", metric_unit="PERCENT")
            spec = ArtifactSpec(
                "percent", "METRICS", "percent.json", False,
                "1.0", ("R",), 24, "fixture.py",
            )
            registry = ArtifactRegistry(root, specs=(spec,))
            record = registry.load_all()["percent"]

            self.assertEqual(record.status, "INCOMPATIBLE")
            self.assertTrue(
                registry.diagnostics()["incompatible_metric_units"]
            )
