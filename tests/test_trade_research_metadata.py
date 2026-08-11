from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import trade_tracker
from research_data_quality import assess_research_trade


METADATA = {
    "timeframe": "1h",
    "market_regime": "TREND",
    "trend_alignment": "ALIGNED",
    "volatility": "NORMAL",
    "confidence": 91,
    "score": 27,
    "quality": "A",
    "primary_blocker": "NOT_APPLICABLE",
    "decision_source": "LIVE_BASELINE",
}


class TradeResearchMetadataTest(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "trades.csv"
        self.trades_file = patch.object(trade_tracker, "TRADES_FILE", self.path)
        self.trades_file.start()
        self.diagnostics = patch.object(trade_tracker, "_research_diagnostics")
        self.diagnostics.start()

    def tearDown(self):
        self.diagnostics.stop()
        self.trades_file.stop()
        self.temp.cleanup()

    def rows(self):
        with self.path.open(newline="") as handle:
            return list(csv.DictReader(handle))

    def test_new_trade_has_stable_id_and_metadata_survives_close(self):
        trade_tracker.open_trade("BTC/USDT", "LONG", 100, 99, 102, research_metadata=METADATA)
        opened = self.rows()[0]
        self.assertTrue(opened["trade_id"].startswith("LIVE-"))
        self.assertEqual("LIVE_PERSISTED", opened["trade_id_provenance"])
        self.assertEqual(METADATA, json.loads(opened["research_metadata_json"]))

        trade_tracker.close_trade("BTC/USDT", "WIN", exit_price=102, pnl=2)
        closed = self.rows()[0]
        self.assertEqual(opened["trade_id"], closed["trade_id"])
        self.assertEqual("102", closed["exit_price"])
        self.assertEqual(METADATA, json.loads(closed["research_metadata_json"]))
        self.assertEqual("COMPLETE", closed["research_data_quality"])

    def test_legacy_trade_gets_deterministic_provenance_without_crash(self):
        legacy_fields = [field for field in trade_tracker.FIELDS if field not in {"trade_id", "trade_id_provenance", "research_metadata_json", "research_data_quality"}]
        with self.path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=legacy_fields)
            writer.writeheader()
            writer.writerow({
                "symbol": "ETH/USDT", "direction": "SHORT", "entry": 200,
                "stop_loss": 202, "take_profit": 196, "status": "OPEN",
                "result": "", "opened_at": "2026-08-11T10:00:00", "closed_at": "",
                "exit_price": "", "pnl": "",
            })

        trade_tracker.close_trade("ETH/USDT", "LOSS", exit_price=202, pnl=-2)
        closed = self.rows()[0]
        self.assertTrue(closed["trade_id"].startswith("LEGACY-"))
        self.assertEqual("LEGACY_DETERMINISTIC", closed["trade_id_provenance"])
        self.assertEqual("LEGACY_PARTIAL", closed["research_data_quality"])

    def test_open_trade_upgrades_legacy_header_without_misalignment(self):
        legacy_fields = [field for field in trade_tracker.FIELDS if field not in {"trade_id", "trade_id_provenance", "research_metadata_json", "research_data_quality"}]
        with self.path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=legacy_fields)
            writer.writeheader()
            writer.writerow({
                "symbol": "ETH/USDT", "direction": "LONG", "entry": 200,
                "stop_loss": 198, "take_profit": 204, "status": "OPEN",
                "result": "", "opened_at": "2026-08-11T10:00:00", "closed_at": "",
                "exit_price": "", "pnl": "",
            })

        trade_tracker.open_trade("BTC/USDT", "LONG", 100, 99, 102, research_metadata=METADATA)
        rows = self.rows()
        self.assertEqual(2, len(rows))
        self.assertEqual("ETH/USDT", rows[0]["symbol"])
        self.assertEqual("BTC/USDT", rows[1]["symbol"])
        self.assertTrue(rows[1]["trade_id"].startswith("LIVE-"))

    def test_missing_optional_context_does_not_become_unknown(self):
        assessment = assess_research_trade({
            "trade_id": "LIVE-1", "trade_id_provenance": "LIVE_PERSISTED",
            "symbol": "BTC/USDT", "direction": "LONG", "entry": 100,
            "opened_at": "2026-08-11T10:00:00", "status": "WIN", "result": "WIN",
            "exit_price": 102,
        })
        self.assertEqual("PARTIAL", assessment["data_quality"])
        self.assertEqual([], assessment["missing_required"])
        self.assertIn("confidence", assessment["missing_optional"])

    def test_missing_identity_is_unavailable(self):
        assessment = assess_research_trade({"status": "LOSS", "result": "LOSS", "exit_price": 90})
        self.assertEqual("UNAVAILABLE", assessment["data_quality"])
        self.assertIn("trade_id", assessment["missing_required"])

    def test_research_assessment_failure_never_blocks_close(self):
        trade_tracker.open_trade("SOL/USDT", "LONG", 100, 99, 102, research_metadata=METADATA)
        with patch("research_data_quality.assess_research_trade", side_effect=RuntimeError("observer unavailable")):
            trade_tracker.close_trade("SOL/USDT", "LOSS", exit_price=99, pnl=-1)
        closed = self.rows()[0]
        self.assertEqual("LOSS", closed["result"])
        self.assertEqual("99", closed["exit_price"])
        self.assertEqual("UNAVAILABLE", closed["research_data_quality"])
