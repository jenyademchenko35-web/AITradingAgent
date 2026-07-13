"""Golden tests for Research Consensus report freshness."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from report_metadata import build_report_metadata, source_file_sha256
from research_consensus.consensus_loader import ConsensusLoader


class ReportFreshnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
        self.trades = self.root / "trades.csv"
        with self.trades.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=(
                    "symbol",
                    "direction",
                    "entry",
                    "stop_loss",
                    "exit_price",
                    "status",
                    "result",
                    "closed_at",
                ),
            )
            writer.writeheader()
            writer.writerow({
                "symbol": "BTC/USDT",
                "direction": "LONG",
                "entry": 100,
                "stop_loss": 90,
                "exit_price": 110,
                "status": "WIN",
                "result": "WIN",
                "closed_at": "2026-07-13T10:00:00+00:00",
            })
        self.source = self.root / "source.csv"
        self.source.write_text("value\n1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_report(
        self,
        *,
        generated_at: datetime | None = None,
        metric_unit: str = "R",
        closed_trades_total: int = 1,
        body_closed_trades: int = 1,
    ) -> Path:
        report_path = self.root / "trade_loss_report.json"
        metadata = build_report_metadata(
            generator="freshness_test",
            metric_unit=metric_unit,
            source_files=[self.source],
            base_dir=self.root,
            generated_at=(generated_at or self.now).isoformat(),
            closed_trades_total=closed_trades_total,
            complete_metrics_total=1,
        )
        report_path.write_text(
            json.dumps({
                "metadata": metadata,
                "sample": {"closed_trades": body_closed_trades},
                "result": "test",
            }),
            encoding="utf-8",
        )
        return report_path

    def load(self) -> ConsensusLoader:
        return ConsensusLoader(
            self.root,
            report_files={"trade_loss": "trade_loss_report.json"},
            ttl_hours=24,
            now=self.now,
            trades_file=self.trades,
        )

    def test_current_compatible_report_is_accepted(self) -> None:
        self.write_report()
        loader = self.load()
        self.assertIn("trade_loss", loader.reports)
        self.assertEqual(loader.source_status["trade_loss"]["state"], "CURRENT")

    def test_old_report_is_stale(self) -> None:
        self.write_report(generated_at=self.now - timedelta(hours=25))
        loader = self.load()
        self.assertNotIn("trade_loss", loader.reports)
        self.assertEqual(loader.source_status["trade_loss"]["state"], "STALE")

    def test_future_report_is_stale(self) -> None:
        self.write_report(generated_at=self.now + timedelta(hours=1))
        loader = self.load()
        self.assertEqual(loader.source_status["trade_loss"]["state"], "STALE")
        self.assertTrue(any(
            "будущем" in reason
            for reason in loader.source_status["trade_loss"]["reasons"]
        ))

    def test_different_metric_unit_is_incompatible(self) -> None:
        self.write_report(metric_unit="PERCENT")
        loader = self.load()
        self.assertEqual(
            loader.source_status["trade_loss"]["state"],
            "INCOMPATIBLE",
        )
        self.assertEqual(len(loader.incompatible_metric_units()), 1)

    def test_unsupported_schema_version_is_incompatible(self) -> None:
        report_path = self.write_report()
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        payload["metadata"]["schema_version"] = "999"
        report_path.write_text(json.dumps(payload), encoding="utf-8")

        loader = self.load()
        self.assertEqual(
            loader.source_status["trade_loss"]["state"],
            "INCOMPATIBLE",
        )

    def test_changed_source_fingerprint_is_stale(self) -> None:
        self.write_report()
        with self.source.open("a", encoding="utf-8") as file:
            file.write("2\n")
        loader = self.load()
        self.assertEqual(loader.source_status["trade_loss"]["state"], "STALE")
        self.assertTrue(any(
            "fingerprint" in reason
            for reason in loader.source_status["trade_loss"]["reasons"]
        ))

    def test_canonical_closed_trade_mismatch_is_stale(self) -> None:
        self.write_report(closed_trades_total=2)
        loader = self.load()
        self.assertEqual(loader.source_status["trade_loss"]["state"], "STALE")
        self.assertTrue(any(
            "closed_trades_total" in reason
            for reason in loader.source_status["trade_loss"]["reasons"]
        ))

    def test_missing_metadata_is_stale(self) -> None:
        report_path = self.root / "trade_loss_report.json"
        report_path.write_text('{"result": "legacy"}', encoding="utf-8")
        loader = self.load()
        self.assertEqual(loader.source_status["trade_loss"]["state"], "STALE")

    def test_missing_source_file_is_stale(self) -> None:
        missing = self.root / "missing.csv"
        report_path = self.root / "trade_loss_report.json"
        metadata = build_report_metadata(
            generator="freshness_test",
            metric_unit="R",
            source_files=[missing],
            base_dir=self.root,
            generated_at=self.now.isoformat(),
            closed_trades_total=1,
            complete_metrics_total=1,
        )
        report_path.write_text(
            json.dumps({"metadata": metadata, "sample": {"closed_trades": 1}}),
            encoding="utf-8",
        )
        loader = self.load()
        self.assertEqual(loader.source_status["trade_loss"]["state"], "STALE")

    def test_payload_sample_mismatch_is_stale(self) -> None:
        self.write_report(body_closed_trades=2)
        loader = self.load()
        self.assertEqual(loader.source_status["trade_loss"]["state"], "STALE")
        self.assertTrue(any(
            "payload closed sample" in reason
            for reason in loader.source_status["trade_loss"]["reasons"]
        ))

    def test_sha256_source_provenance_is_accepted(self) -> None:
        report_path = self.write_report()
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        metadata = payload["metadata"]
        metadata.pop("source_file_fingerprints")
        metadata["source_file_hashes"] = {
            "source.csv": source_file_sha256(self.source)
        }
        report_path.write_text(json.dumps(payload), encoding="utf-8")

        loader = self.load()
        self.assertEqual(loader.source_status["trade_loss"]["state"], "CURRENT")


if __name__ == "__main__":
    unittest.main()
