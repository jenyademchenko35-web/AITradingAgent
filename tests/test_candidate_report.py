import csv
import json
import tempfile
import unittest
from pathlib import Path

from candidate_laboratory import DECISION_FIELDS, TRADE_FIELDS
from candidate_report import build_reports, calculate_metrics


class CandidateReportTest(unittest.TestCase):
    def test_metrics_pf_net_r_drawdown(self):
        metrics = calculate_metrics([
            {"status": "WIN", "pnl_r": 2, "rr": 2},
            {"status": "LOSS", "pnl_r": -1, "rr": 2},
            {"status": "LOSS", "pnl_r": -1, "rr": 2},
            {"status": "WIN", "pnl_r": 2, "rr": 2},
        ])
        self.assertEqual(metrics["profit_factor"], 2)
        self.assertEqual(metrics["net_r"], 2)
        self.assertEqual(metrics["max_drawdown_r"], 2)

    def test_insufficient_data_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.json"
            config.write_text(json.dumps({
                "LIVE_BASELINE": {"enabled": True},
                "OTHER": {"enabled": True},
            }))
            report, comparison = build_reports(
                decisions_path=root / "decisions.csv",
                trades_path=root / "trades.csv", config_path=config,
                report_path=root / "report.json", comparison_path=root / "comparison.json",
            )
            self.assertEqual(report["status"], "INSUFFICIENT_DATA")
            self.assertEqual(
                comparison["comparisons"]["OTHER"]["confidence_status"],
                "INSUFFICIENT_DATA",
            )


if __name__ == "__main__":
    unittest.main()
