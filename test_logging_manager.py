"""Regression tests for current-cycle best-candidate explanations."""

from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from logging_manager import ConsoleOutputManager
from telegram_formatters import SAME_CYCLE, failed_filters_match


class BestSetupCycleTest(TestCase):
    def test_stale_failed_filter_is_not_used_for_current_cycle(self) -> None:
        diagnostics = [
            {
                "timestamp": "2026-07-13T10:00:05+00:00",
                "symbol": "BTC/USDT",
                "direction": "LONG",
                "cycle_id": "old-cycle",
                "stage": "EXECUTION",
                "trend": "FAIL",
                "primary_blocker": "Trend",
            },
            {
                "timestamp": "2026-07-13T11:00:04+00:00",
                "symbol": "BTC/USDT",
                "direction": "LONG",
                "cycle_id": "current-cycle",
                "stage": "FINAL_FILTERS",
                "trend": "FAIL",
                "primary_blocker": "Trend",
            },
            {
                "timestamp": "2026-07-13T11:00:05+00:00",
                "symbol": "BTC/USDT",
                "direction": "LONG",
                "cycle_id": "current-cycle",
                "stage": "EXECUTION",
                "trend": "PASS",
            },
            {
                "timestamp": "2026-07-13T11:00:07+00:00",
                "symbol": "BTC/USDT",
                "direction": "SHORT",
                "cycle_id": "current-cycle",
                "stage": "EXECUTION",
                "trend": "FAIL",
                "primary_blocker": "Trend",
            },
        ]
        explanations = [
            {
                "timestamp": "2026-07-13T10:00:06+00:00",
                "symbol": "BTC/USDT",
                "direction": "LONG",
                "cycle_id": "old-cycle",
                "stage": "EXECUTION",
                "failed_filters": "Trend",
            },
            {
                "timestamp": "2026-07-13T11:00:04+00:00",
                "symbol": "BTC/USDT",
                "direction": "LONG",
                "cycle_id": "current-cycle",
                "stage": "FINAL_FILTERS",
                "failed_filters": "Trend",
            },
            {
                "timestamp": "2026-07-13T11:00:06+00:00",
                "symbol": "BTC/USDT",
                "direction": "LONG",
                "cycle_id": "current-cycle",
                "stage": "EXECUTION",
                "failed_filters": "",
            },
            {
                "timestamp": "2026-07-13T11:00:07+00:00",
                "symbol": "BTC/USDT",
                "direction": "SHORT",
                "cycle_id": "current-cycle",
                "stage": "EXECUTION",
                "failed_filters": "Trend",
            },
        ]
        started = "2026-07-13T11:00:00+00:00"
        finished = "2026-07-13T11:00:10+00:00"

        match = failed_filters_match(
            "BTC/USDT",
            direction="LONG",
            cycle_id="current-cycle",
            stage="EXECUTION",
            cycle_started_at=started,
            cycle_finished_at=finished,
            diagnostics_rows=diagnostics,
            explanation_rows=explanations,
        )
        self.assertEqual(match.quality, SAME_CYCLE)
        self.assertNotIn("Trend", match.filters)

        decision = SimpleNamespace(
            direction="LONG",
            signal="WATCH",
            quality="C",
            score=23,
            confidence=75,
            long_total=23,
            short_total=5,
            summary="current cycle",
            raw_signal_status="WATCH",
            final_filter_status="PASSED",
            execution_status="ELIGIBLE",
            failed_filters=[],
            veto_reasons=[],
            cycle_id="current-cycle",
            decision_timestamp="",
            stage="EXECUTION",
        )

        def rows_for(path):
            return diagnostics if path.name == "decision_diagnostics.csv" else explanations

        output = StringIO()
        with patch("telegram_formatters.read_csv_rows", side_effect=rows_for):
            with redirect_stdout(output):
                ConsoleOutputManager().best_setup(
                    "BTC/USDT",
                    decision,
                    started,
                    finished,
                )

        rendered = output.getvalue()
        self.assertIn("Match quality: SAME_CYCLE", rendered)
        self.assertNotIn("Не хватает Trend", rendered)
