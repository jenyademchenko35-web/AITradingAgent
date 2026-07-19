from __future__ import annotations

import json
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest import TestCase

from execution_simulator import ExecutionSimulator, format_execution


class FakeRegistry:
    def __init__(self, trades):
        self.trades = trades

    def get_complete_trades(self):
        return [deepcopy(row) for row in self.trades]


def sample_trade(result_r=2.0):
    return {"trade_id": "T1", "symbol": "BTC/USDT", "direction": "LONG", "entry": 100.0,
            "exit": 120.0 if result_r > 0 else 90.0, "sl": 90.0, "R": result_r, "pnl": result_r * 10}


class ExecutionSimulatorTest(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "reports").mkdir()
        (root / "reports" / "portfolio_manager.json").write_text('{"status":"READY"}', encoding="utf-8")
        (root / "reports" / "research_dashboard.json").write_text('{"system_research_status":{"status":"OK"}}', encoding="utf-8")
        self.trades = [sample_trade(2), {**sample_trade(-1), "trade_id": "T2", "exit": 90, "R": -1}]
        self.simulator = ExecutionSimulator(registry=FakeRegistry(self.trades), base_dir=root,
            report_path=root / "reports/execution_simulator.json",
            summary_path=root / "reports/execution_simulator_summary.txt",
            history_dir=root / "reports/execution_history")

    def tearDown(self):
        self.temp.cleanup()

    def test_commission_reduces_adjusted_r_and_pnl(self):
        row = self.simulator.simulate_trade(sample_trade(), mode="NORMAL", delay_ms=0)
        self.assertGreater(row["commission"], 0)
        self.assertLess(row["adjusted_r"], row["original_r"])
        self.assertLess(row["adjusted_pnl"], row["original_pnl"])

    def test_slippage_is_adverse_to_long_and_short(self):
        long_row = self.simulator.simulate_trade(sample_trade(), delay_ms=0)
        short_trade = {**sample_trade(), "direction": "SHORT", "exit": 80, "sl": 110}
        short_row = self.simulator.simulate_trade(short_trade, delay_ms=0)
        self.assertGreater(long_row["executed_entry"], 100)
        self.assertLess(short_row["executed_entry"], 100)

    def test_delay_worsens_execution(self):
        immediate = self.simulator.simulate_trade(sample_trade(), delay_ms=0)
        delayed = self.simulator.simulate_trade(sample_trade(), delay_ms=1000)
        self.assertGreater(delayed["executed_entry"], immediate["executed_entry"])
        self.assertLess(delayed["adjusted_r"], immediate["adjusted_r"])

    def test_partial_fill_scales_result_and_risk(self):
        full = self.simulator.simulate_trade(sample_trade(), fill_pct=100, delay_ms=0)
        half = self.simulator.simulate_trade(sample_trade(), fill_pct=50, delay_ms=0)
        self.assertAlmostEqual(half["adjusted_r"], full["adjusted_r"] / 2, places=5)
        self.assertAlmostEqual(half["adjusted_pnl"], full["adjusted_pnl"] / 2, places=5)

    def test_gap_up_and_down_are_supported(self):
        up = self.simulator.simulate_trade(sample_trade(), mode="STRESS", gap_direction="UP")
        down = self.simulator.simulate_trade(sample_trade(), mode="STRESS", gap_direction="DOWN")
        self.assertEqual(up["gap"], "GAP_UP")
        self.assertEqual(down["gap"], "GAP_DOWN")

    def test_stress_and_extreme_are_increasingly_adverse(self):
        normal = self.simulator.simulate_trade(sample_trade(), mode="NORMAL")
        stress = self.simulator.simulate_trade(sample_trade(), mode="STRESS")
        extreme = self.simulator.simulate_trade(sample_trade(), mode="EXTREME")
        self.assertGreater(stress["fee_pct"], normal["fee_pct"])
        self.assertGreater(extreme["slippage_pct"], stress["slippage_pct"])
        self.assertGreater(stress["execution_delay_ms"], normal["execution_delay_ms"])

    def test_robustness_score_and_verdict_are_bounded(self):
        report = self.simulator.run("NORMAL")
        self.assertGreaterEqual(report["robustness_score"], 0)
        self.assertLessEqual(report["robustness_score"], 100)
        self.assertIn(report["status"], {"ROBUST", "ACCEPTABLE", "FRAGILE"})
        self.assertEqual(report["robustness_breakdown"]["partial_fill_penalty"], 0)

    def test_partial_fill_penalizes_stress_robustness(self):
        stress = self.simulator.run("STRESS")
        extreme = self.simulator.run("EXTREME")
        self.assertGreater(stress["robustness_breakdown"]["partial_fill_penalty"], 0)
        self.assertGreater(extreme["robustness_breakdown"]["partial_fill_penalty"], stress["robustness_breakdown"]["partial_fill_penalty"])

    def test_reports_and_history_deduplicate(self):
        first, created = self.simulator.write_reports("NORMAL")
        second, duplicate_created = self.simulator.write_reports("NORMAL")
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first["dataset_fingerprint"], second["dataset_fingerprint"])
        self.assertEqual(len(list(self.simulator.history_dir.glob("*.json"))), 1)
        self.assertTrue(self.simulator.report_path.exists())
        self.assertTrue(self.simulator.summary_path.exists())

    def test_formatter_and_telegram_registration(self):
        report = self.simulator.run()
        self.assertIn("⚙️ Execution Simulator", format_execution(report))
        self.assertIn("Robustness", format_execution(report, "summary"))
        from telegram_handlers import BOT_COMMANDS_V5
        import telegram_bot_v4
        self.assertIn("execution", {item.command for item in BOT_COMMANDS_V5})
        self.assertTrue(callable(telegram_bot_v4.execution_command))

    def test_source_trades_are_not_modified(self):
        original = deepcopy(self.trades)
        self.simulator.run("EXTREME")
        self.assertEqual(self.trades, original)

    def test_source_context_is_read_only_and_reported(self):
        report = self.simulator.run()
        self.assertEqual(report["source_health"]["portfolio_manager"], "OK")
        self.assertEqual(report["source_health"]["research_dashboard"], "OK")
        self.assertIn("READ_ONLY", report["restrictions"])
