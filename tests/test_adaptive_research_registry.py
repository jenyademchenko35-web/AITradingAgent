"""Artifact gate and confidence-fusion regression tests."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from adaptive_research.artifact_registry import ArtifactRegistry, SPEC_BY_KEY
from adaptive_research.recommendation_engine import AdaptiveRecommendationEngine
from adaptive_research.state import build_trade_fingerprint
from report_metadata import build_report_metadata


def write_trade(path: Path, exit_price: float = 102.0) -> None:
    fields = [
        "symbol", "direction", "entry", "stop_loss", "take_profit",
        "exit_price", "status", "result", "opened_at", "closed_at",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "symbol": "BTC/USDT",
            "direction": "LONG",
            "entry": 100,
            "stop_loss": 99,
            "take_profit": 102,
            "exit_price": exit_price,
            "status": "CLOSED",
            "result": "WIN" if exit_price > 100 else "LOSS",
            "opened_at": "2026-07-13T00:00:00+00:00",
            "closed_at": "2026-07-13T01:00:00+00:00",
        })


def write_metrics_report(
    root: Path,
    *,
    schema: str = "1.0",
    unit: str = "R",
    generated_at: str | None = None,
) -> None:
    metadata = build_report_metadata(
        generator="test",
        generator_version="1.0",
        schema_version=schema,
        metric_unit=unit,
        source_files=[root / "trades.csv"],
        base_dir=root,
        closed_trades_total=1,
        complete_metrics_total=1,
        generated_at=generated_at,
    )
    payload = {
        "generated_at": metadata["generated_at"],
        "metadata": metadata,
        "status": "OK",
        "metrics": {"closed_trades": 1, "metrics_trades": 1},
    }
    (root / "trade_metrics_audit.json").write_text(json.dumps(payload), encoding="utf-8")


class AdaptiveArtifactRegistryTest(unittest.TestCase):
    def test_fresh_compatible_report_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_trade(root / "trades.csv")
            write_metrics_report(root)
            registry = ArtifactRegistry(root, build_trade_fingerprint(root / "trades.csv"))
            record = registry.inspect(SPEC_BY_KEY["metrics"], newly_generated=True)
        self.assertEqual(record.status, "CURRENT")
        self.assertTrue(record.accepted)
        self.assertTrue(record.hash)

    def test_changed_source_fingerprint_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_trade(root / "trades.csv")
            write_metrics_report(root)
            write_trade(root / "trades.csv", exit_price=99.0)
            registry = ArtifactRegistry(root, build_trade_fingerprint(root / "trades.csv"))
            record = registry.inspect(SPEC_BY_KEY["metrics"])
        self.assertEqual(record.status, "STALE")
        self.assertFalse(record.accepted)
        self.assertTrue(any("fingerprint mismatch" in reason for reason in record.reasons))

    def test_schema_and_metric_unit_mismatch_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_trade(root / "trades.csv")
            write_metrics_report(root, schema="9.9", unit="PERCENT")
            registry = ArtifactRegistry(root, build_trade_fingerprint(root / "trades.csv"))
            record = registry.inspect(SPEC_BY_KEY["metrics"], newly_generated=True)
        self.assertEqual(record.status, "IGNORED")
        self.assertFalse(record.accepted)
        self.assertTrue(any("schema_version" in reason for reason in record.reasons))
        self.assertTrue(any("metric_unit" in reason for reason in record.reasons))

    def test_artifact_mutation_without_new_created_at_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_trade(root / "trades.csv")
            write_metrics_report(root)
            fingerprint = build_trade_fingerprint(root / "trades.csv")
            initial_registry = ArtifactRegistry(root, fingerprint)
            initial = initial_registry.inspect(
                SPEC_BY_KEY["metrics"],
                newly_generated=True,
            )
            path = root / "trade_metrics_audit.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["status"] = "MUTATED"
            path.write_text(json.dumps(payload), encoding="utf-8")
            registry = ArtifactRegistry(
                root,
                fingerprint,
                previous_records={"metrics": initial.to_dict()},
            )
            record = registry.inspect(SPEC_BY_KEY["metrics"])
        self.assertEqual(record.status, "IGNORED")
        self.assertTrue(any("artifact hash mismatch" in reason for reason in record.reasons))

    def test_expired_report_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_trade(root / "trades.csv")
            old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
            write_metrics_report(root, generated_at=old)
            registry = ArtifactRegistry(root, build_trade_fingerprint(root / "trades.csv"))
            record = registry.inspect(SPEC_BY_KEY["metrics"])
        self.assertEqual(record.status, "STALE")
        self.assertTrue(any("TTL" in reason for reason in record.reasons))

    def test_confidence_fusion_is_conservative_below_fifty_trades(self) -> None:
        payloads = {
            "replay": {
                "status": "OK",
                "metadata": {"complete_metrics_total": 30},
                "metrics": {"effective_portfolio": {"profit_factor": 1.4, "net_r": 4}},
                "recommendation": "Продолжить replay.",
            },
            "lab": {
                "status": "OK",
                "metadata": {"complete_metrics_total": 30},
                "leader": {"hypothesis": "Edge 16"},
                "recommendation": "Продолжить shadow.",
            },
        }
        result = AdaptiveRecommendationEngine().build(payloads, closed_trades=30)
        self.assertEqual(result["status"], "COLLECT_MORE_DATA")
        self.assertLess(result["global_confidence"], 1.0)
        self.assertIn("50", result["primary"])
        self.assertFalse(any("включить" in item.lower() for item in result["forbidden_actions"]))


if __name__ == "__main__":
    unittest.main()
