"""Research Orchestrator compatibility tests for Shadow Replay v2."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from report_metadata import build_report_metadata
from research_orchestrator.artifact_registry import DEFAULT_ARTIFACTS, ArtifactRegistry
from research_orchestrator.evidence_gate import EvidenceBuilder
from research_orchestrator.models import ArtifactSpec


class ShadowReplayResearchIntegrationTest(TestCase):
    def test_registry_accepts_fresh_schema_two_replay_as_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "trades.csv"
            source.write_text("symbol,status\nBTC/USDT,WIN\n", encoding="utf-8")
            self._write_audit(root, source)
            self._write_shadow(root, source)
            specs = self._specs()

            registry = ArtifactRegistry(root, specs=specs)
            records = registry.load_all()
            evidence, _, _ = EvidenceBuilder().build(registry.accepted_payloads())

            self.assertEqual(records["shadow_replay_report"].status, "VALID")
            self.assertTrue(any(
                row.source == "shadow_replay_report" for row in evidence
            ))

    def test_stale_or_schema_mismatched_replay_is_not_accepted(self) -> None:
        now = datetime.now(timezone.utc)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "trades.csv"
            source.write_text("symbol,status\nBTC/USDT,WIN\n", encoding="utf-8")
            self._write_audit(root, source, generated_at=now.isoformat())
            self._write_shadow(
                root,
                source,
                generated_at=(now - timedelta(hours=73)).isoformat(),
            )
            stale = ArtifactRegistry(
                root, specs=self._specs(), now=now
            ).load_all()["shadow_replay_report"]
            self.assertEqual(stale.status, "STALE")

            payload = json.loads(
                (root / "shadow_replay_report.json").read_text(encoding="utf-8")
            )
            payload["metadata"]["generated_at"] = now.isoformat()
            payload["metadata"]["schema_version"] = "1.0"
            (root / "shadow_replay_report.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            incompatible = ArtifactRegistry(
                root, specs=self._specs(), now=now
            ).load_all()["shadow_replay_report"]
            self.assertEqual(incompatible.status, "INCOMPATIBLE")

    def test_default_registry_declares_shadow_schema_two(self) -> None:
        spec = next(
            item for item in DEFAULT_ARTIFACTS
            if item.artifact_name == "shadow_replay_report"
        )
        self.assertEqual(spec.schema_version, "2.0")
        self.assertEqual(spec.metric_units, ("R",))

    @staticmethod
    def _specs() -> tuple[ArtifactSpec, ArtifactSpec]:
        return (
            ArtifactSpec(
                "trade_metrics_audit", "METRICS", "trade_metrics_audit.json",
                True, "1.0", ("R",), 24, "fixture.py", True,
            ),
            ArtifactSpec(
                "shadow_replay_report", "REPLAY", "shadow_replay_report.json",
                False, "2.0", ("R",), 72, "shadow_replay.py", True,
            ),
        )

    @staticmethod
    def _write_audit(
        root: Path,
        source: Path,
        *,
        generated_at: str | None = None,
    ) -> None:
        metadata = build_report_metadata(
            generator="fixture",
            generator_version="1.0",
            schema_version="1.0",
            metric_unit="R",
            source_files=(source,),
            base_dir=root,
            closed_trades_total=1,
            complete_metrics_total=1,
            generated_at=generated_at,
        )
        payload = {
            "metadata": metadata,
            "metrics": {
                "closed_trades": 1,
                "metrics_trades": 1,
                "profit_factor": 1.0,
                "net_r": 1.0,
                "max_drawdown_r": 0.0,
            },
        }
        (root / "trade_metrics_audit.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    @staticmethod
    def _write_shadow(
        root: Path,
        source: Path,
        *,
        generated_at: str | None = None,
    ) -> None:
        metadata = build_report_metadata(
            generator="shadow_replay",
            generator_version="2.0",
            schema_version="2.0",
            metric_unit="R",
            source_files=(source,),
            base_dir=root,
            closed_trades_total=1,
            complete_metrics_total=1,
            generated_at=generated_at,
        )
        metadata["normalizer_version"] = "1.0"
        payload = {
            "metadata": metadata,
            "sample": {"complete_metrics_total": 1},
            "metrics": {
                "ideal_all": {"profit_factor": 1.0, "net_r": 1.0},
                "effective_portfolio": {
                    "trades": 1, "profit_factor": 0.9, "net_r": 0.8,
                },
                "impact": {"total_execution_impact_r": -0.2},
            },
        }
        (root / "shadow_replay_report.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
