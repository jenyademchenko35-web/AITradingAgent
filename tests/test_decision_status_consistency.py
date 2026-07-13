"""Regression tests for raw/filter/execution decision status separation."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest import TestCase

from best_candidate_ranker import normalize_candidate
from decision_diagnostics import DecisionDiagnostics
from logging_manager import ConsoleOutputManager


class DecisionStatusConsistencyTest(TestCase):
    def test_failed_filters_and_cooldown_remain_separate(self) -> None:
        decision = SimpleNamespace(
            direction="LONG",
            signal="HIGH PRIORITY",
            score=29,
            confidence=100,
            quality="A",
            long_total=29,
            short_total=8,
            summary="LONG wins",
            cycle_id="cycle-1",
            decision_timestamp="2026-07-13T12:00:00+00:00",
            raw_signal_status="HIGH PRIORITY",
            execution_status="ELIGIBLE",
            veto_reasons=[],
        )
        report = DecisionDiagnostics(auto_log=False).analyze(
            decision,
            SimpleNamespace(long=20, short=5),
            SimpleNamespace(long=5, short=10),
            SimpleNamespace(long=12, short=4),
            SimpleNamespace(long=0, short=20),
        )

        DecisionDiagnostics.set_execution_status(
            decision,
            "BLOCKED_COOLDOWN",
            "Cooldown active for SOL_USDT_LONG",
        )
        DecisionDiagnostics.sync_report_status(report, decision)
        candidate = normalize_candidate(("SOL/USDT", decision))

        self.assertEqual(report["raw_signal_status"], "HIGH PRIORITY")
        self.assertEqual(report["final_filter_status"], "BLOCKED_FILTERS")
        self.assertEqual(report["execution_status"], "BLOCKED_COOLDOWN")
        self.assertEqual(report["failed_filters"], ["Structure", "Risk"])
        self.assertEqual(
            report["veto_reasons"], ["Cooldown active for SOL_USDT_LONG"]
        )
        self.assertEqual(report["stage"], "EXECUTION")
        self.assertEqual(candidate.status, "BLOCKED_COOLDOWN")
        self.assertEqual(candidate.raw_signal_status, "HIGH PRIORITY")

        output = StringIO()
        with redirect_stdout(output):
            ConsoleOutputManager().symbol_summary("SOL/USDT", decision, 0.1)
        rendered = output.getvalue()
        self.assertIn("Raw signal=HIGH PRIORITY", rendered)
        self.assertIn("Final filters=BLOCKED_FILTERS", rendered)
        self.assertIn("Execution=BLOCKED_COOLDOWN", rendered)
        self.assertIn("Failed=Structure, Risk", rendered)
        self.assertIn("Veto=Cooldown active", rendered)


if __name__ == "__main__":
    import unittest

    unittest.main()
