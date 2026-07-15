from __future__ import annotations

import json
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import AsyncMock

from experiment_promotion_engine import ExperimentPromotionEngine


class ExperimentPromotionEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _metadata(
        self,
        closed: int,
        *,
        schema: str = "1.0",
        age_hours: float = 1,
    ) -> dict[str, object]:
        return {
            "schema_version": schema,
            "generated_at": (self.now - timedelta(hours=age_hours)).isoformat(),
            "metric_unit": "R",
            "closed_trades_total": closed,
            "complete_metrics_total": closed,
            "freshness_ttl_hours": 24,
        }

    def _write_sources(
        self,
        closed: int,
        *,
        overfiltered: bool = False,
        age_hours: float = 1,
    ) -> None:
        baseline = {
            "hypothesis": "baseline",
            "group": "baseline",
            "trades": closed,
            "profit_factor": 0.8,
            "net_r": -4.0,
            "max_drawdown_r": 8.0,
        }
        candidate = {
            "hypothesis": "Momentum Confirmation",
            "group": "momentum",
            "trades": 0 if overfiltered else max(10, closed - 5),
            "wins": 0 if overfiltered else 20,
            "losses": 0 if overfiltered else 25,
            "profit_factor": 0 if overfiltered else 1.35,
            "net_r": 0 if overfiltered else 5.5,
            "max_drawdown_r": 4.0,
            "saved_losses": 8,
            "lost_winners": 2 if not overfiltered else 5,
            "verdict": "OVERFILTERED" if overfiltered else "PROMISING",
        }
        reports = {
            "hypothesis_report.json": {
                "metadata": self._metadata(closed, age_hours=age_hours),
                "baseline": baseline,
                "metrics": [baseline, candidate],
            },
            "shadow_replay_report.json": {
                "metadata": self._metadata(
                    closed, schema="2.0", age_hours=age_hours
                ),
                "replay_metrics": {
                    "ideal_profit_factor": 1.4,
                    "real_profit_factor": 1.25,
                    "ideal_net_r": 6.0,
                    "real_net_r": 4.5,
                },
            },
            "adaptive_research_report.json": {
                "metadata": self._metadata(closed, age_hours=age_hours),
                "recommendation": {
                    "leader": "Momentum Confirmation",
                    "global_confidence": 0.82,
                    "findings": [],
                },
            },
            "research_orchestrator_report.json": {
                "metadata": self._metadata(closed, age_hours=age_hours),
                "hypotheses": [{
                    "hypothesis": "Momentum Confirmation",
                    "status": "PROMISING",
                    "supporting_sources": ["hypothesis_report"],
                    "contradicting_sources": [],
                    "reasons": ["Эффект подтверждён независимым анализом."],
                    "warnings": [],
                }],
            },
        }
        for filename, payload in reports.items():
            (self.root / filename).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )

    def _engine(self) -> ExperimentPromotionEngine:
        return ExperimentPromotionEngine(self.root, now=self.now)

    def test_gate_remains_closed_below_fifty_trades(self) -> None:
        self._write_sources(49)
        report = self._engine().build_report()

        self.assertEqual("INSUFFICIENT_DATA", report["status"])
        self.assertEqual(1, report["remaining_closed_trades"])
        self.assertEqual([], report["promotion_candidates"])
        self.assertFalse(report["candidates"][0]["eligible_for_ab_test"])

    def test_supported_hypothesis_becomes_ab_candidate_at_threshold(self) -> None:
        self._write_sources(50)
        report = self._engine().build_report()
        candidate = report["candidates"][0]

        self.assertEqual("CANDIDATES_AVAILABLE", report["status"])
        self.assertEqual("Momentum Confirmation", candidate["candidate"])
        self.assertEqual("CANDIDATE_FOR_AB_TEST", candidate["promotion_status"])
        self.assertTrue(candidate["eligible_for_ab_test"])
        self.assertGreaterEqual(candidate["confidence"], 65)
        self.assertFalse(candidate["apply_automatically"])
        self.assertFalse(report["apply_automatically"])

    def test_overfiltered_hypothesis_is_never_promoted(self) -> None:
        self._write_sources(80, overfiltered=True)
        report = self._engine().build_report()
        candidate = report["candidates"][0]

        self.assertEqual("DO_NOT_PROMOTE", candidate["promotion_status"])
        self.assertEqual("HIGH", candidate["risk"])
        self.assertFalse(candidate["eligible_for_ab_test"])

    def test_missing_and_stale_reports_do_not_crash_or_promote(self) -> None:
        self._write_sources(60, age_hours=30)
        (self.root / "adaptive_research_report.json").unlink()
        report = self._engine().build_report()

        self.assertEqual("DATA_NOT_READY", report["status"])
        self.assertEqual("MISSING", report["source_reports"]["adaptive_research"]["status"])
        self.assertEqual("STALE", report["source_reports"]["strategy_lab"]["status"])
        self.assertEqual([], report["promotion_candidates"])

    def test_run_writes_only_promotion_artifacts(self) -> None:
        self._write_sources(49)
        before = {path.name for path in self.root.iterdir()}
        report = self._engine().run()
        after = {path.name for path in self.root.iterdir()}

        self.assertEqual(
            {"experiment_promotion_report.json", "experiment_promotion_summary.txt"},
            after - before,
        )
        saved = json.loads(
            (self.root / "experiment_promotion_report.json").read_text(encoding="utf-8")
        )
        self.assertEqual(report["status"], saved["status"])


class ExperimentPromotionTelegramTest(unittest.TestCase):
    def test_formatter_reads_ready_report_and_shows_details(self) -> None:
        import telegram_bot_v4

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiment_promotion_report.json"
            path.write_text(json.dumps({
                "status": "INSUFFICIENT_DATA",
                "closed_trades_total": 48,
                "minimum_closed_trades": 50,
                "promotion_candidates": [],
                "candidates": [{
                    "candidate": "Momentum Confirmation",
                    "confidence": 61.5,
                    "risk": "MEDIUM",
                    "promotion_status": "INSUFFICIENT_DATA",
                    "reasons_for": ["PF выше baseline."],
                    "reasons_against": ["Нужно ещё 2 сделки."],
                }],
                "recommendation": "Продолжить сбор данных.",
            }), encoding="utf-8")
            with patch.object(
                telegram_bot_v4, "EXPERIMENT_PROMOTION_REPORT_FILE", path
            ):
                text = telegram_bot_v4.format_promotion(["details"])

        self.assertIn("Momentum Confirmation", text)
        self.assertIn("Причины за", text)
        self.assertIn("Причины против", text)
        self.assertIn("не применяются", text)

    def test_command_replies_once_and_does_not_run_research(self) -> None:
        import telegram_bot_v4

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiment_promotion_report.json"
            path.write_text(json.dumps({
                "status": "INSUFFICIENT_DATA",
                "closed_trades_total": 48,
                "minimum_closed_trades": 50,
                "promotion_candidates": [],
                "candidates": [],
                "recommendation": "Продолжить сбор данных.",
            }), encoding="utf-8")
            reply = AsyncMock()
            context = SimpleNamespace(args=["details"])
            with (
                patch.object(
                    telegram_bot_v4, "EXPERIMENT_PROMOTION_REPORT_FILE", path
                ),
                patch.object(telegram_bot_v4, "reply", reply),
                patch("subprocess.run") as subprocess_run,
            ):
                asyncio.run(telegram_bot_v4.promotion_command(None, context))

        reply.assert_awaited_once()
        subprocess_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
