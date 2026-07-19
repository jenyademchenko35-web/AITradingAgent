"""Tests for the read-only Trade Registry Single Source of Truth."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from adaptive_research.state import build_trade_fingerprint
from shadow_replay.engine import ShadowReplayEngine
from strategy_lab.engine import StrategyLabEngine
from trade_registry import (
    LIFECYCLE_STATUSES,
    TradeRegistry,
    get_all_trades,
    get_closed_trades,
    get_complete_trades,
    get_open_trades,
    get_statistics,
    get_trade,
    save_reports,
    validate_trade,
)


FIELDS = [
    "trade_id",
    "symbol",
    "direction",
    "entry",
    "stop_loss",
    "take_profit",
    "status",
    "result",
    "opened_at",
    "closed_at",
    "exit_price",
    "rr",
    "archived",
]


def complete_trade(trade_id: str = "T-1", opened_at: str = "2026-01-01T00:00:00+00:00") -> dict[str, str]:
    closed_at = (datetime.fromisoformat(opened_at) + timedelta(hours=2)).isoformat()
    return {
        "trade_id": trade_id,
        "symbol": "BTC/USDT",
        "direction": "LONG",
        "entry": "100",
        "stop_loss": "90",
        "take_profit": "120",
        "status": "WIN",
        "result": "WIN",
        "opened_at": opened_at,
        "closed_at": closed_at,
        "exit_price": "120",
        "rr": "2",
        "archived": "",
    }


def write_trades(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


class TradeRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.path = self.base / "trades.csv"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_classifies_complete_open_incomplete_invalid_and_archived(self) -> None:
        complete = complete_trade()
        opened = complete_trade("T-OPEN", "2026-01-02T00:00:00+00:00")
        opened.update({"status": "OPEN", "result": "", "closed_at": "", "exit_price": ""})
        incomplete = complete_trade("T-INCOMPLETE", "2026-01-03T00:00:00+00:00")
        incomplete["exit_price"] = ""
        invalid = complete_trade("T-INVALID", "2026-01-04T00:00:00+00:00")
        invalid["direction"] = "SIDEWAYS"
        archived = complete_trade("T-ARCHIVED", "2026-01-05T00:00:00+00:00")
        archived["archived"] = "true"
        write_trades(self.path, [complete, opened, incomplete, invalid, archived])

        statuses = {row["trade_id"]: row["status"] for row in TradeRegistry(self.path).get_all_trades()}
        self.assertEqual("COMPLETE", statuses["T-1"])
        self.assertEqual("OPEN", statuses["T-OPEN"])
        self.assertEqual("INCOMPLETE", statuses["T-INCOMPLETE"])
        self.assertEqual("INVALID", statuses["T-INVALID"])
        self.assertEqual("ARCHIVED", statuses["T-ARCHIVED"])
        self.assertTrue(set(statuses.values()).issubset(LIFECYCLE_STATUSES))

    def test_detects_missing_required_fields(self) -> None:
        row = complete_trade()
        row["exit_price"] = ""
        registry = TradeRegistry(self.path, rows=[row])
        issues = registry.get_issues()
        self.assertTrue(any(issue["error_type"] == "missing_fields" and issue["severity"] == "HIGH" for issue in issues))

    def test_detects_duplicate_trade(self) -> None:
        first = complete_trade("T-1")
        second = complete_trade("T-2")
        registry = TradeRegistry(self.path, rows=[first, second])
        self.assertTrue(any(issue["error_type"] == "duplicate_trade" for issue in registry.get_issues()))
        self.assertEqual(1, registry.get_statistics()["invalid_trades"])

    def test_handles_corrupt_prices_time_and_zero_risk(self) -> None:
        row = complete_trade()
        row.update({"entry": "0", "stop_loss": "0", "closed_at": "2025-12-31T23:00:00+00:00"})
        registry = TradeRegistry(self.path, rows=[row])
        types = {issue["error_type"] for issue in registry.get_issues()}
        self.assertIn("invalid_price", types)
        self.assertIn("zero_risk", types)
        self.assertIn("negative_duration", types)
        self.assertIn("time_order_error", types)
        self.assertEqual("INVALID", registry.get_all_trades()[0]["status"])

    def test_detects_wrong_rr(self) -> None:
        row = complete_trade()
        row["rr"] = "5"
        issues = validate_trade(row)
        self.assertTrue(any(issue["error_type"] == "wrong_rr" for issue in issues))

    def test_completion_percentage_uses_closed_sample(self) -> None:
        complete = complete_trade("T-1")
        incomplete = complete_trade("T-2", "2026-01-02T00:00:00+00:00")
        incomplete["exit_price"] = ""
        opened = complete_trade("T-3", "2026-01-03T00:00:00+00:00")
        opened.update({"status": "OPEN", "result": "", "closed_at": "", "exit_price": ""})
        registry = TradeRegistry(self.path, rows=[complete, incomplete, opened])
        stats = registry.get_statistics()
        self.assertEqual(3, stats["total_trades"])
        self.assertEqual(2, stats["closed_trades"])
        self.assertEqual(1, stats["complete_trades"])
        self.assertEqual(50.0, stats["completion_percent"])

    def test_public_api_returns_consistent_registry_sample(self) -> None:
        complete = complete_trade("T-1")
        opened = complete_trade("T-2", "2026-01-02T00:00:00+00:00")
        opened.update({"status": "OPEN", "result": "", "closed_at": "", "exit_price": ""})
        write_trades(self.path, [complete, opened])
        all_rows = get_all_trades(self.path)
        self.assertEqual(2, len(all_rows))
        self.assertEqual(1, len(get_complete_trades(self.path)))
        self.assertEqual(1, len(get_closed_trades(self.path)))
        self.assertEqual(1, len(get_open_trades(self.path)))
        self.assertEqual("T-1", get_trade("T-1", self.path)["trade_id"])
        self.assertEqual(100.0, get_statistics(self.path)["completion_percent"])

    def test_research_modules_share_registry_closed_and_complete_counts(self) -> None:
        rows = [complete_trade("T-1")]
        incomplete = complete_trade("T-2", "2026-01-02T00:00:00+00:00")
        incomplete["exit_price"] = ""
        rows.append(incomplete)
        write_trades(self.path, rows)
        registry = TradeRegistry(self.path)

        lab = StrategyLabEngine(base_dir=self.base, strategies=[])
        opportunities = lab.build_opportunities()
        replay = ShadowReplayEngine(base_dir=self.base).build_report()
        fingerprint = build_trade_fingerprint(self.path)

        self.assertEqual(registry.get_statistics()["complete_trades"], len(opportunities))
        self.assertEqual(registry.get_statistics()["complete_trades"], replay["sample"]["complete_metrics_total"])
        self.assertEqual(registry.get_statistics()["closed_trades"], fingerprint.trade_count)

    def test_reports_and_history_are_read_only_and_deduplicated(self) -> None:
        write_trades(self.path, [complete_trade()])
        original = self.path.read_bytes()
        registry = TradeRegistry(self.path)
        registry_report, quality_report = registry.build_reports()
        reports = self.base / "reports"
        first = save_reports(
            registry_report,
            quality_report,
            registry_path=reports / "trade_registry.json",
            quality_path=reports / "data_quality.json",
            summary_path=reports / "data_quality_summary.txt",
            history_dir=reports / "data_quality_history",
        )
        second = save_reports(
            registry_report,
            quality_report,
            registry_path=reports / "trade_registry.json",
            quality_path=reports / "data_quality.json",
            summary_path=reports / "data_quality_summary.txt",
            history_dir=reports / "data_quality_history",
        )
        self.assertEqual(original, self.path.read_bytes())
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertFalse(registry_report["restrictions"]["automatic_application"])
        self.assertTrue(registry_report["restrictions"]["trading_logic_unchanged"])

    def test_telegram_formatter_and_command_registration(self) -> None:
        registry = TradeRegistry(self.path, rows=[complete_trade()])
        _, quality_report = registry.build_reports()
        import telegram_bot_v4

        with patch("telegram_bot_v4.read_quality_report", return_value=quality_report):
            text = telegram_bot_v4.format_dataquality()
        self.assertIn("📋 Data Quality", text)
        self.assertIn("Complete: 1", text)
        commands = {command.command for command in telegram_bot_v4.BOT_COMMANDS}
        self.assertIn("dataquality", commands)


if __name__ == "__main__":
    unittest.main()
