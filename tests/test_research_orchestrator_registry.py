"""Artifact Registry regression tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from research_orchestrator.artifact_registry import ArtifactRegistry
from research_orchestrator.models import ArtifactSpec
from tests.orchestrator_helpers import write_report


class ArtifactRegistryTest(TestCase):
    def test_fresh_compatible_report_is_accepted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_report(root, "fresh.json", metric_unit="R")
            spec = ArtifactSpec(
                "fresh", "METRICS", "fresh.json", True,
                "1.0", ("R",), 24, "fixture.py",
            )
            registry = ArtifactRegistry(
                root,
                specs=(spec,),
                now=datetime.now(timezone.utc),
            )

            record = registry.load_all()["fresh"]

            self.assertEqual(record.status, "VALID")
            self.assertTrue(record.accepted)
            self.assertIn("fresh", registry.accepted_payloads())

    def test_missing_metadata_is_legacy_not_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "legacy.json").write_text(
                '{"generated_at": "2026-07-13T00:00:00+00:00"}',
                encoding="utf-8",
            )
            spec = ArtifactSpec(
                "legacy", "HYPOTHESIS", "legacy.json", False,
                "1.0", ("R",), 24, "legacy.py",
            )
            registry = ArtifactRegistry(root, specs=(spec,))

            record = registry.load_all()["legacy"]

            self.assertEqual(record.status, "LEGACY")
            self.assertNotIn("legacy", registry.accepted_payloads())
