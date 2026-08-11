from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path
from unittest import TestCase

from research_data_quality import (
    RESEARCH_FIELDS,
    ResearchDataQuality,
    assess_research_trade,
    build_decision_snapshot,
    coverage_report,
    validate_research_trade,
)


class FakeRegistry:
    def __init__(self, rows): self.rows = rows
    def get_complete_trades(self): return [dict(row) for row in self.rows]


def trade(**overrides):
    row = {"trade_id":"T1","symbol":"BTC/USDT","direction":"SHORT","opened_at":"2026-01-02T10:00:00+00:00",
           "entry":100,"exit_price":98,"sl":101,"tp":98,"result":"WIN","R":2}
    row.update(overrides); return row


class ResearchDataQualityTest(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name); (self.root/"reports").mkdir()

    def tearDown(self): self.temp.cleanup()

    def sources(self):
        return {
            "trade_registry":[trade()],
            "decision_snapshot":[{"trade_id":"T1","confidence":91,"score":27,"quality":"A","timeframe":"1h"}],
            "trade_market_context":[{"trade_id":"T1","symbol":"BTC/USDT","direction":"SHORT","trend":"SHORT","volatility":"HIGH","market_regime":"TREND","primary_blocker":"NONE"}],
            "setup_history":[],"decision_log":[],"signals":[],
        }

    def test_validator_lists_only_missing_required_research_fields(self):
        missing=validate_research_trade(trade())
        self.assertNotIn("confidence",missing); self.assertNotIn("market_regime",missing)
        self.assertNotIn("entry_price",missing); self.assertNotIn("risk_reward",missing)

    def test_missing_context_is_partial_not_unknown_or_unavailable(self):
        assessment = assess_research_trade(trade())
        self.assertEqual("PARTIAL", assessment["data_quality"])
        self.assertIn("confidence", assessment["missing_optional"])
        self.assertEqual([], assessment["missing_required"])

    def test_unknown_recovery_uses_snapshot_then_context(self):
        engine=ResearchDataQuality(base_dir=self.root,registry=FakeRegistry([trade()]),sources=self.sources())
        row,recovered=engine.recover_trade(trade(confidence="UNKNOWN"))
        self.assertEqual(row["confidence"],91); self.assertEqual(recovered["confidence"],"decision_snapshot")
        self.assertEqual(row["trend_alignment"],"ALIGNED"); self.assertEqual(row["market_regime"],"TREND")

    def test_coverage_distinguishes_missing_unknown_and_null(self):
        report=coverage_report([{"confidence":90},{"confidence":"UNKNOWN"},{"confidence":None},{}],("confidence",))
        feature=report["features"][0]
        self.assertEqual(feature["coverage_pct"],25); self.assertEqual(feature["missing_pct"],25)
        self.assertEqual(feature["unknown_pct"],25); self.assertEqual(feature["null_pct"],25)

    def test_backfill_writes_schema_valid_derived_artifacts_only(self):
        engine=ResearchDataQuality(base_dir=self.root,registry=FakeRegistry([trade()]),sources=self.sources())
        rows,report=engine.backfill(write=True)
        self.assertEqual(report["complete_trades"],1); self.assertTrue(report["source_trades_unchanged"])
        payload=json.loads((self.root/"reports/research_trades_enriched.json").read_text())
        self.assertEqual(payload["schema_version"],1); self.assertEqual(len(payload["trades"]),1)
        self.assertTrue(set(RESEARCH_FIELDS).issubset(payload["trades"][0]))

    def test_snapshot_generator_uses_existing_decision_log(self):
        fields=["timestamp","symbol","direction","score","confidence","quality","trend_long","trend_short","structure_long","structure_short","momentum_long","momentum_short","risk_long","risk_short","summary"]
        with (self.root/"decision_debug.csv").open("w",newline="") as handle:
            writer=csv.DictWriter(handle,fieldnames=fields); writer.writeheader(); writer.writerow({"timestamp":"2026-01-02T09:59:00+00:00","symbol":"BTC/USDT","direction":"SHORT","score":27,"confidence":91,"quality":"A","summary":"test"})
        snapshot=build_decision_snapshot(base_dir=self.root,symbol="BTC/USDT",opened_at="2026-01-02T10:00:00+00:00")
        self.assertEqual(snapshot["final_score"],27); self.assertEqual(snapshot["confidence"],91)
        payload=json.loads((self.root/"decision_snapshot.json").read_text()); self.assertEqual(payload["schema_version"],1)

    def test_telegram_commands_are_registered(self):
        from telegram_handlers import BOT_COMMANDS_V5
        commands={item.command for item in BOT_COMMANDS_V5}
        self.assertTrue({"dataquality","coverage","backfill","snapshot"}.issubset(commands))
        import telegram_bot_v4
        self.assertTrue(callable(telegram_bot_v4.coverage_command)); self.assertTrue(callable(telegram_bot_v4.backfill_command))
