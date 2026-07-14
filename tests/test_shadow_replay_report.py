"""End-to-end report regression test for Shadow Replay v2."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from shadow_replay.engine import ShadowReplayEngine
from shadow_replay.report import format_summary


class ShadowReplayReportTest(TestCase):
    def test_engine_writes_schema_two_json_text_and_csv(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with (root / "trades.csv").open("w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=(
                    "symbol", "direction", "entry", "stop_loss", "take_profit",
                    "status", "result", "opened_at", "closed_at", "exit_price", "pnl",
                ))
                writer.writeheader()
                writer.writerow({
                    "symbol": "BTC/USDT",
                    "direction": "LONG",
                    "entry": 100,
                    "stop_loss": 90,
                    "take_profit": 120,
                    "status": "WIN",
                    "result": "WIN",
                    "opened_at": "2026-01-01T00:00:00+00:00",
                    "closed_at": "2026-01-01T08:00:00+00:00",
                    "exit_price": 120,
                    "pnl": 20,
                })

            report = ShadowReplayEngine(root).run()

            self.assertTrue((root / "shadow_replay_report.json").exists())
            self.assertTrue((root / "shadow_replay_summary.txt").exists())
            self.assertTrue((root / "shadow_replay_trades.csv").exists())
            stored = json.loads(
                (root / "shadow_replay_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(stored["metadata"]["schema_version"], "2.0")
            self.assertEqual(stored["metadata"]["metric_unit"], "R")
            self.assertEqual(stored["sample"]["complete_metrics_total"], 1)
            self.assertEqual(len(stored["latency_scenarios"]), 5)
            self.assertIn("real_profit_factor", stored["replay_metrics"])
            self.assertIn("ideal_net_r", stored["replay_metrics"])
            self.assertIn(
                "effective_profit_factor", stored["execution_quality"]
            )
            self.assertIn("Shadow Replay Engine v2", format_summary(report))
