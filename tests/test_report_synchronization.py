from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from report_synchronization import (
    build_consistency_report,
    datasource_status,
    report_is_current,
    synchronize_reports,
)


FIELDS = ["symbol", "direction", "entry", "exit", "stop_loss", "take_profit", "opened_at", "closed_at", "result", "R", "status"]


def write_trades(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerow({"symbol": "BTC/USDT", "direction": "LONG", "entry": 100, "exit": 110,
                         "stop_loss": 95, "take_profit": 110, "opened_at": "2026-01-01T00:00:00Z",
                         "closed_at": "2026-01-01T01:00:00Z", "result": "WIN", "R": 2, "status": "CLOSED"})


class ReportSynchronizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        write_trades(self.root / "trades.csv")
        (self.root / "reports").mkdir()
        (self.root / "reports/decision_engine_v2.json").write_text('{"comparisons": []}', encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_pipeline_generates_provenance_audit_and_consistent_metrics(self) -> None:
        result = synchronize_reports(base_dir=self.root, force=True)
        self.assertEqual(["trades", "research_dashboard", "promotion_gate", "decision_intelligence"], result["reports"])
        self.assertEqual("PASSED", result["consistency"]["status"])
        self.assertEqual(1, result["consistency"]["comparisons"]["Closed Trades"]["stats"])
        for relative in ("reports/research_dashboard.json", "reports/promotion_gate.json", "decision_learning.json"):
            payload = json.loads((self.root / relative).read_text(encoding="utf-8"))
            self.assertTrue(payload["generated_at"])
            self.assertTrue(payload["source_trades_modified"])
        audit = json.loads((self.root / "reports/data_source_audit.json").read_text(encoding="utf-8"))
        self.assertEqual("trades.csv", audit["commands"]["stats"]["source"])

    def test_newer_trades_invalidates_every_report(self) -> None:
        synchronize_reports(base_dir=self.root, force=True)
        dashboard = self.root / "reports/research_dashboard.json"
        self.assertTrue(report_is_current(dashboard, self.root / "trades.csv"))
        write_trades(self.root / "trades.csv")
        self.assertFalse(report_is_current(dashboard, self.root / "trades.csv"))
        self.assertTrue(synchronize_reports(base_dir=self.root)["refreshed"])

    def test_datasources_exposes_sampling_contract(self) -> None:
        synchronize_reports(base_dir=self.root, force=True)
        rows = datasource_status(base_dir=self.root)
        self.assertEqual("UP TO DATE", rows["research_dashboard"]["status"])
        self.assertIn("COMPLETE", rows["decision_intelligence"]["sample"])

    def test_consistency_report_records_metric_mismatch(self) -> None:
        synchronize_reports(base_dir=self.root, force=True)
        path = self.root / "reports/research_dashboard.json"
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        dashboard["baseline_metrics"]["closed_trades"] = 42
        path.write_text(json.dumps(dashboard), encoding="utf-8")
        report = build_consistency_report(base_dir=self.root)
        self.assertEqual("FAILED", report["status"])
        self.assertEqual("Closed Trades", report["metric"])


if __name__ == "__main__":
    unittest.main()
