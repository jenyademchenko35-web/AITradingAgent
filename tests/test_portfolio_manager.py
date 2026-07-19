from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase

from portfolio_manager import PortfolioManager, format_portfolio


class FakeRegistry:
    def __init__(self, trades=None):
        self.trades = list(trades or [])

    def get_open_trades(self):
        return [dict(row) for row in self.trades]


def trade(symbol="BTC/USDT", direction="LONG", risk_pct=1.0):
    return {"symbol": symbol, "direction": direction, "risk_pct": risk_pct, "status": "OPEN"}


class PortfolioManagerTest(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = root / "portfolio_config.json"
        self.groups = root / "correlation_groups.json"
        self.report = root / "reports" / "portfolio_manager.json"
        self.summary = root / "reports" / "portfolio_manager_summary.txt"
        self.config.write_text(json.dumps({
            "MAX_OPEN_TRADES": 3,
            "MAX_PORTFOLIO_RISK": 3.0,
            "MAX_SYMBOL_RISK": 1.0,
            "MAX_CORRELATED_GROUP_RISK": 2.0,
            "ALLOW_HEDGE": False,
        }), encoding="utf-8")
        self.groups.write_text(json.dumps({"BTC": ["BTC", "ETH"], "L1": ["SOL", "AVAX"]}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def manager(self, positions=()):
        return PortfolioManager(registry=FakeRegistry(positions), config_path=self.config,
                                groups_path=self.groups, report_path=self.report,
                                summary_path=self.summary)

    def test_allows_trade_inside_limits_and_uses_registry(self):
        manager = self.manager([trade("SOL/USDT", risk_pct=.5)])
        result = manager.can_open_trade(trade("BTC/USDT", risk_pct=.8))
        self.assertEqual(result["status"], "ALLOW")
        self.assertEqual(manager.portfolio_summary()["open_trades"], 1)

    def test_blocks_total_portfolio_risk(self):
        result = self.manager([trade("SOL", risk_pct=1.2), trade("DOGE", risk_pct=1.2)]).can_open_trade(trade("ETH", risk_pct=.8))
        self.assertIn("MAX_PORTFOLIO_RISK", result["reasons"])

    def test_blocks_max_open_trades(self):
        result = self.manager([trade("SOL"), trade("DOGE"), trade("XRP")]).can_open_trade(trade("ETH"))
        self.assertIn("MAX_OPEN_TRADES", result["reasons"])

    def test_blocks_duplicate_position(self):
        result = self.manager([trade("BTC/USDT", "LONG")]).can_open_trade(trade("BTC", "LONG"))
        self.assertIn("DUPLICATE_POSITION", result["reasons"])

    def test_blocks_hedge(self):
        result = self.manager([trade("BTC", "LONG")]).can_open_trade(trade("BTC", "SHORT"))
        self.assertIn("HEDGE_NOT_ALLOWED", result["reasons"])

    def test_blocks_symbol_risk(self):
        result = self.manager().can_open_trade(trade("BTC", risk_pct=1.1))
        self.assertIn("MAX_SYMBOL_RISK", result["reasons"])

    def test_blocks_correlated_group_risk(self):
        result = self.manager([trade("BTC", risk_pct=1.0)]).can_open_trade(trade("ETH", risk_pct=1.1))
        self.assertIn("GROUP_RISK_LIMIT", result["reasons"])

    def test_selects_best_signal_in_required_order(self):
        signals = [
            {"symbol": "BTC", "confidence": 80, "score": 25, "expected_r": 2, "age_seconds": 20},
            {"symbol": "ETH", "confidence": 90, "score": 20, "expected_r": 1, "age_seconds": 50},
            {"symbol": "SOL", "confidence": 90, "score": 20, "expected_r": 1, "age_seconds": 10},
        ]
        self.assertEqual(self.manager().select_best_signal(signals)["symbol"], "SOL")

    def test_reports_and_telegram_formatter(self):
        report = self.manager([trade("BTC", "LONG")]).write_reports()
        self.assertTrue(self.report.exists())
        self.assertTrue(self.summary.exists())
        self.assertIn("📊 Portfolio", format_portfolio(report))
        self.assertIn("Portfolio Risk", format_portfolio(report, "risk"))
        self.assertIn("OPEN LONG", format_portfolio(report, "positions"))

    def test_gate_does_not_mutate_signal(self):
        signal = {"symbol": "ETH", "direction": "LONG", "confidence": 91, "entry": 10, "stop_loss": 9, "risk_pct": .5}
        original = dict(signal)
        self.manager().can_open_trade(signal)
        self.assertEqual(signal, original)

    def test_telegram_command_is_registered(self):
        from telegram_handlers import BOT_COMMANDS_V5
        import telegram_bot_v4
        self.assertIn("portfolio", {command.command for command in BOT_COMMANDS_V5})
        self.assertTrue(callable(telegram_bot_v4.portfolio_command))

