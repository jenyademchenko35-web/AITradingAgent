"""Tests for the Shadow-only walk-forward validator and Telegram view."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from strategy_lab.hypotheses.edge_threshold import EdgeThreshold
from walk_forward_validator import (
    VERDICTS,
    build_report,
    build_windows,
    save_report,
)


def opportunity(index: int, result: str, r_value: float) -> dict[str, object]:
    """Build one deterministic completed Strategy Lab opportunity."""
    return {
        "id": f"trade-{index:02d}",
        "timestamp": f"2026-01-{index + 1:02d}T00:00:00+00:00",
        "symbol": "BTC/USDT",
        "direction": "LONG",
        "result": result,
        "actual_r": r_value,
        "rr": 2.0,
        "edge": 25,
        "duration_hours": 2.0,
    }


class WalkForwardValidatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            opportunity(index, "WIN" if index % 3 == 0 else "LOSS", 2.0 if index % 3 == 0 else -1.0)
            for index in range(15)
        ]

    def test_windows_are_expanding_and_forward_only(self) -> None:
        windows = build_windows(
            len(self.rows),
            test_size=3,
            minimum_train_size=6,
        )
        self.assertEqual(3, len(windows))
        self.assertEqual((0, 6, 6, 9), (
            windows[0].train_start,
            windows[0].train_end,
            windows[0].test_start,
            windows[0].test_end,
        ))
        for previous, current in zip(windows, windows[1:]):
            self.assertEqual(previous.test_end, current.test_start)
            self.assertEqual(0, current.train_start)
            self.assertEqual(current.train_end, current.test_start)

    def test_report_uses_only_allowed_verdicts_and_shadow_restrictions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = build_report(
                base_dir=Path(directory),
                opportunities=list(reversed(self.rows)),
                hypotheses=[EdgeThreshold(20)],
                max_hypotheses=1,
                test_size=3,
                minimum_train_size=6,
            )
        self.assertEqual("OK", report["status"])
        self.assertEqual("SHADOW_RESEARCH_ONLY", report["mode"])
        self.assertTrue(report["sample"]["chronological_split"])
        self.assertFalse(report["sample"]["random_shuffle"])
        self.assertIn("closed_trades_total", report["sample"])
        self.assertIn("incomplete_metrics_total", report["sample"])
        self.assertEqual(1, report["hypotheses_count"])
        result = report["hypotheses"][0]
        self.assertIn(result["verdict"], VERDICTS)
        self.assertGreaterEqual(result["stability_score"], 0)
        self.assertLessEqual(result["stability_score"], 100)
        self.assertEqual(3, result["windows"])
        self.assertTrue(report["restrictions"]["live_unchanged"])
        self.assertTrue(report["restrictions"]["decision_engine_unchanged"])
        self.assertFalse(report["restrictions"]["automatic_application"])

    def test_reports_and_telegram_formatter_share_contract(self) -> None:
        report = build_report(
            opportunities=self.rows,
            hypotheses=[EdgeThreshold(20)],
            max_hypotheses=1,
            test_size=3,
            minimum_train_size=6,
        )
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "reports" / "walk_forward.json"
            summary_path = Path(directory) / "reports" / "walk_forward_summary.txt"
            save_report(report, report_json=report_path, summary_txt=summary_path)
            self.assertEqual(report, json.loads(report_path.read_text(encoding="utf-8")))
            summary = summary_path.read_text(encoding="utf-8")
            self.assertIn("📈 Walk Forward", summary)
            self.assertIn("READY_FOR_AB:", summary)

            from telegram_bot_v4 import format_walkforward

            with patch("telegram_bot_v4.WALK_FORWARD_REPORT_FILE", report_path):
                message = format_walkforward()
            self.assertIn("Hypotheses: 1", message)
            self.assertIn("Recommendation:", message)


if __name__ == "__main__":
    unittest.main()
