from __future__ import annotations

import tempfile
from copy import deepcopy
from pathlib import Path
from unittest import TestCase

from signal_quality_analyzer import FEATURES, RECOMMENDATIONS, SignalQualityAnalyzer, format_signal_quality


class FakeRegistry:
    def __init__(self, rows): self.rows=rows
    def get_complete_trades(self): return deepcopy(self.rows)


def sample():
    rows=[]; contexts=[]
    for i in range(20):
        win=i%3==0; symbol="BTC/USDT" if win else "ETH/USDT"; opened=f"2026-06-{i+1:02d}T{8+i%10:02d}:00:00+00:00"
        rows.append({"trade_id":f"T{i}","symbol":symbol,"direction":"LONG" if win else "SHORT","entry":100,"sl":90,"tp":120,"R":2 if win else -1,"opened_at":opened})
        contexts.append({"symbol":symbol,"opened_at":opened,"confidence":92 if win else 72,"score":29 if win else 24,"quality":"A" if win else "C","trend":"LONG","volatility":.5 if win else 1.5,"atr":1 if win else 3,"primary_blocker":"NONE" if win else "MOMENTUM","market_regime":"TREND" if win else "RANGE","entry_type":"BREAKOUT" if win else "REBOUND"})
    return rows,contexts


class SignalQualityAnalyzerTest(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); root=Path(self.temp.name); (root/"reports").mkdir()
        self.rows,self.contexts=sample()
        self.analyzer=SignalQualityAnalyzer(registry=FakeRegistry(self.rows),base_dir=root,context_rows=self.contexts,
            report_path=root/"reports/signal_quality.json",summary_path=root/"reports/signal_quality_summary.txt",history_dir=root/"reports/signal_quality_history")

    def tearDown(self): self.temp.cleanup()

    def test_feature_score_is_bounded_and_rewards_separation(self):
        report=self.analyzer.build_report(); by={x["feature"]:x for x in report["feature_ranking"]}
        self.assertGreater(by["confidence"]["feature_score"],0)
        self.assertLessEqual(by["confidence"]["feature_score"],100)
        self.assertIn("score_breakdown",by["confidence"])

    def test_winning_and_losing_profiles(self):
        report=self.analyzer.build_report(); win=report["winning_profile"]; loss=report["losing_profile"]
        self.assertGreater(win["average_confidence"],loss["average_confidence"])
        self.assertGreater(win["average_score"],loss["average_score"])
        self.assertEqual(win["quality"][0]["value"],"A")

    def test_feature_ranking_has_all_features_and_rank(self):
        ranking=self.analyzer.build_report()["feature_ranking"]
        self.assertEqual({x["feature"] for x in ranking},set(FEATURES))
        self.assertEqual([x["rank"] for x in ranking],list(range(1,len(FEATURES)+1)))

    def test_drift_detection(self):
        drift=self.analyzer.build_report()["drift"]
        self.assertIn(drift["status"],{"IMPROVING","STABLE","DEGRADING","UNKNOWN"})
        self.assertIn("first_metrics",drift); self.assertIn("last_metrics",drift)

    def test_unknown_reduces_coverage_and_predictive_power(self):
        contexts=deepcopy(self.contexts)
        for row in contexts: row["confidence"]=""
        analyzer=SignalQualityAnalyzer(registry=FakeRegistry(self.rows),base_dir=Path(self.temp.name),context_rows=contexts)
        confidence=next(x for x in analyzer.build_report()["feature_ranking"] if x["feature"]=="confidence")
        self.assertEqual(confidence["coverage_pct"],0); self.assertEqual(confidence["predictive_power"],"NONE")
        self.assertEqual(confidence["recommendation"],"INSUFFICIENT_DATA")

    def test_candidate_filters_are_research_only(self):
        report=self.analyzer.build_report(); self.assertTrue(report["candidate_filters"])
        self.assertTrue(all(x["recommendation"] in RECOMMENDATIONS for x in report["candidate_filters"]))
        self.assertIn("NO_AUTOMATIC_APPLICATION",report["restrictions"])

    def test_reports_and_history_deduplicate(self):
        _,first=self.analyzer.write_reports(); _,second=self.analyzer.write_reports()
        self.assertTrue(first); self.assertFalse(second)
        self.assertTrue(self.analyzer.report_path.exists()); self.assertTrue(self.analyzer.summary_path.exists())
        self.assertEqual(len(list(self.analyzer.history_dir.glob("*.json"))),1)

    def test_formatter_and_telegram_commands(self):
        report=self.analyzer.build_report()
        for view in ("","top","features","drift"): self.assertIn("Signal Quality",format_signal_quality(report,view))
        from telegram_handlers import BOT_COMMANDS_V5
        import telegram_bot_v4
        self.assertIn("quality",{x.command for x in BOT_COMMANDS_V5}); self.assertTrue(callable(telegram_bot_v4.quality_command))

    def test_source_trades_are_not_modified(self):
        original=deepcopy(self.rows); self.analyzer.build_report(); self.assertEqual(self.rows,original)

    def test_signal_quality_score_and_status(self):
        report=self.analyzer.build_report()
        self.assertGreaterEqual(report["signal_quality_score"],0); self.assertLessEqual(report["signal_quality_score"],100)
        self.assertIn(report["signal_quality_status"],{"EXCELLENT","GOOD","AVERAGE","POOR"})
        self.assertIn(report["recommendation"],RECOMMENDATIONS)
