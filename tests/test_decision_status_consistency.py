"""Regression tests for raw/filter/execution decision status separation."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from best_candidate_ranker import normalize_candidate
from decision_diagnostics import DecisionDiagnostics
from logging_manager import ConsoleOutputManager
import multi_timeframe_agent_v3 as agent_v3


class DecisionStatusConsistencyTest(TestCase):
    def test_blocked_portfolio_is_normal_blocked_outcome(self) -> None:
        decision = SimpleNamespace(
            direction="LONG",
            signal="SETUP",
            score=27,
            confidence=90,
            quality="A",
            long_total=27,
            short_total=10,
            summary="LONG wins",
            raw_signal_status="SETUP",
            final_filter_status="PASSED",
            execution_status="ELIGIBLE",
            veto_reasons=[],
            failed_filters=[],
            cycle_id="cycle-portfolio",
            decision_timestamp="2026-07-30T12:00:00+00:00",
            stage="FINAL_FILTERS",
        )
        reason = "Max portfolio exposure reached"

        DecisionDiagnostics.set_execution_status(
            decision, "BLOCKED_PORTFOLIO", reason
        )

        self.assertEqual(decision.execution_status, "BLOCKED_PORTFOLIO")
        self.assertEqual(decision.veto_reasons, [reason])
        self.assertEqual(decision.stage, "EXECUTION")
        candidate = normalize_candidate(("BTC/USDT", decision))
        self.assertEqual(candidate.status, "BLOCKED_PORTFOLIO")
        self.assertEqual(candidate.execution_status, "BLOCKED_PORTFOLIO")
        self.assertEqual(candidate.veto_reasons, (reason,))

        diagnostics = DecisionDiagnostics(auto_log=False)
        csv_row = diagnostics._csv_row({
            "symbol": "BTC/USDT",
            "execution_status": decision.execution_status,
            "veto_reasons": decision.veto_reasons,
        })
        self.assertEqual(csv_row["execution_status"], "BLOCKED_PORTFOLIO")
        self.assertEqual(csv_row["veto_reasons"], reason)

        output = StringIO()
        with redirect_stdout(output):
            ConsoleOutputManager().symbol_summary("BTC/USDT", decision, 0.1)
        rendered = output.getvalue()
        self.assertIn("Execution=BLOCKED_PORTFOLIO", rendered)
        self.assertIn(f"Veto={reason}", rendered)

    def test_other_blocked_statuses_remain_supported(self) -> None:
        for status in (
            "BLOCKED_COOLDOWN",
            "BLOCKED_DUPLICATE",
            "BLOCKED_HIGHER_TF",
            "BLOCKED_FILTERS",
        ):
            with self.subTest(status=status):
                decision = SimpleNamespace(veto_reasons=[])
                DecisionDiagnostics.set_execution_status(
                    decision, status, "existing behavior"
                )
                self.assertEqual(decision.execution_status, status)
                self.assertEqual(decision.veto_reasons, ["existing behavior"])

    def test_blocked_portfolio_does_not_increment_error_counter(self) -> None:
        decision = SimpleNamespace(
            direction="LONG",
            signal="SETUP",
            score=27,
            confidence=90,
            quality="A",
            long_total=27,
            short_total=10,
            summary="Portfolio veto",
            raw_signal_status="SETUP",
            final_filter_status="PASSED",
            execution_status="BLOCKED_PORTFOLIO",
            veto_reasons=["Portfolio exposure limit"],
            failed_filters=[],
        )
        market = SimpleNamespace(tf1h=SimpleNamespace(close=100.0))
        captured = {}

        def capture_stats(decisions, api_errors):
            captured["decisions"] = decisions
            captured["api_errors"] = api_errors

        with (
            patch.object(agent_v3, "SYMBOLS", ["BTC/USDT"]),
            patch.object(agent_v3, "_AGENT_SINGLETON_LOCK", SimpleNamespace(acquired=True)),
            patch.object(agent_v3, "process_notification_outbox"),
            patch.object(agent_v3, "analyze_symbol", return_value=(decision, market)),
            patch.object(agent_v3, "update_stats", side_effect=capture_stats),
            patch.object(agent_v3, "select_best_candidate", return_value=None),
            patch.object(agent_v3, "update_open_trades") as update_open,
            patch.object(agent_v3.LOGGER, "symbol_summary"),
            patch.object(agent_v3.LOGGER, "ranked_summary"),
            patch.object(agent_v3.LOGGER, "signal_counts"),
            patch.object(agent_v3.LOGGER, "symbol_error") as symbol_error,
        ):
            agent_v3.run_once()

        self.assertEqual(captured["api_errors"], 0)
        self.assertEqual(captured["decisions"], [("BTC/USDT", decision)])
        symbol_error.assert_not_called()
        update_open.assert_called_once_with({"BTC/USDT": 100.0})

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
