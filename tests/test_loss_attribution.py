from __future__ import annotations

import tempfile
from copy import deepcopy
from pathlib import Path
from unittest import TestCase

from loss_attribution import DIMENSIONS, LossAttribution, RECOMMENDATIONS, format_loss_analysis


class FakeRegistry:
    def __init__(self, trades): self.trades = trades
    def get_complete_trades(self): return deepcopy(self.trades)


def trades():
    values = [-1, -1, 2, -1, -1, -1, 2, -1, 2, 1]
    return [{"trade_id": f"T{i}", "symbol": "BTC/USDT" if i < 6 else "ETH/USDT",
             "direction": "SHORT" if i < 6 else "LONG", "R": value,
             "result": "LOSS" if value < 0 else "WIN",
             "opened_at": f"2026-06-{i + 1:02d}T{8 + i:02d}:00:00+00:00"}
            for i, value in enumerate(values)]


def contexts(source):
    rows = []
    for i, row in enumerate(source):
        rows.append({"symbol": row["symbol"], "opened_at": row["opened_at"],
                     "confidence": 65 if i < 6 else 85, "score": 21 if i < 6 else 27,
                     "quality": "C" if i < 6 else "A", "volatility": .7 if i < 6 else 1.5,
                     "trend": "LONG", "primary_blocker": "Momentum" if i < 6 else "",
                     "market_regime": "RANGE" if i < 6 else "TREND", "timeframe": "1h"})
    return rows


class LossAttributionTest(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        self.source = trades()
        self.analyzer = LossAttribution(registry=FakeRegistry(self.source), base_dir=root,
            context_rows=contexts(self.source), report_path=root/"reports/loss_attribution.json",
            summary_path=root/"reports/loss_attribution_summary.txt",
            history_dir=root/"reports/loss_attribution_history", minimum_filter_sample=3)

    def tearDown(self): self.temp.cleanup()

    def test_uses_complete_registry_sample_and_all_dimensions(self):
        report = self.analyzer.build_report()
        self.assertEqual(report["sample"]["complete_trades"], 10)
        self.assertEqual(set(report["dimensions"]), set(DIMENSIONS))

    def test_segment_metrics_and_loss_contribution(self):
        report = self.analyzer.build_report()
        btc = next(x for x in report["dimensions"]["symbol"] if "BTC" in x["segment"])
        self.assertEqual(btc["trades"], 6); self.assertEqual(btc["net_r"], -3)
        self.assertGreater(btc["loss_contribution_pct"], 0)
        self.assertAlmostEqual(btc["sample_share_pct"], 60)

    def test_time_weekday_streak_and_exit_classification(self):
        report = self.analyzer.build_report()
        self.assertTrue(report["dimensions"]["hour_of_day"])
        self.assertTrue(report["dimensions"]["weekday"])
        self.assertTrue(any("2-3" in x["segment"] for x in report["dimensions"]["consecutive_losses"]))
        self.assertTrue(any("STOP_LOSS" in x["segment"] for x in report["dimensions"]["exit_type"]))

    def test_pareto_analysis_is_present_and_bounded(self):
        pareto = self.analyzer.build_report()["pareto"]
        self.assertGreater(pareto["negative_scenarios"], 0)
        self.assertGreater(len(pareto["scenarios_to_reach_80pct"]), 0)
        self.assertLessEqual(pareto["loss_share_from_top_20pct_pct"], 100)

    def test_counterfactual_reports_removed_losses_and_winners(self):
        filters = self.analyzer.build_report()["counterfactual_filters"]
        candidate = next(x for x in filters if x["minimum_sample_passed"])
        self.assertGreater(candidate["removed_losing_trades"], 0)
        self.assertGreater(candidate["net_r_change"], 0)
        self.assertIn(candidate["recommendation"], RECOMMENDATIONS)

    def test_small_samples_are_insufficient(self):
        filters = self.analyzer.build_report()["counterfactual_filters"]
        self.assertTrue(all(x["recommendation"] == "INSUFFICIENT_DATA" for x in filters if x["sample"] < 3))

    def test_factor_combinations_are_analyzed(self):
        names = {tuple(x["fields"].keys()) for x in self.analyzer.build_report()["combinations"]}
        self.assertIn(("symbol", "direction"), names)
        self.assertIn(("trend_alignment", "confidence"), names)

    def test_reports_and_history_are_deduplicated(self):
        _, first = self.analyzer.write_reports(); _, second = self.analyzer.write_reports()
        self.assertTrue(first); self.assertFalse(second)
        self.assertTrue(self.analyzer.report_path.exists()); self.assertTrue(self.analyzer.summary_path.exists())
        self.assertEqual(len(list(self.analyzer.history_dir.glob("*.json"))), 1)

    def test_source_trades_are_not_modified(self):
        original = deepcopy(self.source); self.analyzer.build_report(); self.assertEqual(self.source, original)

    def test_formatter_and_telegram_registration(self):
        report = self.analyzer.build_report()
        self.assertIn("Loss Attribution", format_loss_analysis(report))
        self.assertIn("Top scenarios", format_loss_analysis(report, "top"))
        self.assertIn("Filters", format_loss_analysis(report, "filters"))
        from telegram_handlers import BOT_COMMANDS_V5
        import telegram_bot_v4
        self.assertIn("lossanalysis", {item.command for item in BOT_COMMANDS_V5})
        self.assertTrue(callable(telegram_bot_v4.lossanalysis_command))

    def test_only_allowed_recommendations_are_emitted(self):
        report = self.analyzer.build_report()
        emitted = {report["status"]} | {x["recommendation"] for x in report["counterfactual_filters"]}
        self.assertLessEqual(emitted, RECOMMENDATIONS)
        self.assertIn("NO_AUTOMATIC_FILTER_APPLICATION", report["restrictions"])
