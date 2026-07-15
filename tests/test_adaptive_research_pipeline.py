"""Pipeline, incremental and stage-isolation tests for Adaptive Research."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from adaptive_research.engine import AdaptiveResearchEngine
from adaptive_research.pipeline import (
    STAGE_SPECS,
    AdaptivePipeline,
    StageResult,
)
from adaptive_research.scheduler import AdaptiveResearchScheduler
from report_metadata import build_report_metadata


FIELDS = [
    "symbol",
    "direction",
    "entry",
    "stop_loss",
    "take_profit",
    "exit_price",
    "status",
    "result",
    "opened_at",
    "closed_at",
]


def write_trades(path: Path, count: int) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        for index in range(count):
            writer.writerow({
                "symbol": f"TEST{index}/USDT",
                "direction": "LONG",
                "entry": 100,
                "stop_loss": 99,
                "take_profit": 102,
                "exit_price": 102 if index % 2 == 0 else 99,
                "status": "CLOSED",
                "result": "WIN" if index % 2 == 0 else "LOSS",
                "opened_at": f"2026-07-{index + 1:02d}T00:00:00+00:00",
                "closed_at": f"2026-07-{index + 1:02d}T01:00:00+00:00",
            })


class FakePipeline:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.calls = 0

    def run(self, only: str | None = None) -> list[StageResult]:
        self.calls += 1
        with (self.base_dir / "trades.csv").open(encoding="utf-8") as file:
            trade_count = sum(1 for _ in csv.DictReader(file))
        selected = [stage for stage in STAGE_SPECS if only in (None, stage.key)]
        results = []
        for stage in selected:
            schema = "2.0" if stage.key == "replay" else "1.0"
            metadata = build_report_metadata(
                generator=f"tests.{stage.key}",
                generator_version=schema,
                schema_version=schema,
                metric_unit="R",
                source_files=[self.base_dir / "trades.csv"],
                base_dir=self.base_dir,
                closed_trades_total=trade_count,
                complete_metrics_total=trade_count,
            )
            payload = self._payload(stage.key, metadata, trade_count)
            (self.base_dir / stage.report).write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            results.append(StageResult(
                key=stage.key,
                label=stage.label,
                status="OK",
                started_at=metadata["generated_at"],
                finished_at=metadata["generated_at"],
                duration_seconds=0.01,
                report=stage.report,
                return_code=0,
            ))
        return results

    @staticmethod
    def _payload(key: str, metadata: dict, count: int) -> dict:
        common = {"generated_at": metadata["generated_at"], "metadata": metadata, "status": "OK"}
        if key == "metrics":
            common["metrics"] = {"closed_trades": count, "metrics_trades": count}
        elif key == "loss":
            common.update({"sample": {"closed_trades": count}, "conclusion": "Наблюдать LOSS."})
        elif key == "memory":
            common.update({"matches_count": count, "recommendation": "Продолжить наблюдение."})
        elif key == "replay":
            common.update({
                "sample": {"complete_metrics_total": count},
                "metrics": {"effective_portfolio": {"profit_factor": 1.1, "net_r": 1.0}},
                "recommendation": "Продолжить replay.",
            })
        elif key == "lab":
            common.update({
                "opportunities": count,
                "leader": None,
                "ranking": [{"hypothesis": "Edge 16", "trades": count}],
                "recommendation": "Продолжить shadow.",
            })
        elif key == "orchestrator":
            common["recommendation"] = {"primary": "Продолжить research."}
        return common


class RaisingExecutor:
    def execute(self, stage, base_dir):  # type: ignore[no-untyped-def]
        if stage.key == "loss":
            raise RuntimeError("controlled failure")
        return StageResult(
            key=stage.key,
            label=stage.label,
            status="OK",
            started_at="2026-07-14T00:00:00+00:00",
            finished_at="2026-07-14T00:00:01+00:00",
            duration_seconds=1.0,
            report=stage.report,
            return_code=0,
        )


class AdaptiveResearchPipelineTest(unittest.TestCase):
    def test_incremental_run_skips_unchanged_closed_trades(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_trades(root / "trades.csv", 1)
            pipeline = FakePipeline(root)
            engine = AdaptiveResearchEngine(root, pipeline=pipeline)  # type: ignore[arg-type]

            first = engine.run()
            second = engine.run()
            write_trades(root / "trades.csv", 2)
            third = engine.run()

            self.assertEqual(first["status"], "OK")
            self.assertTrue(first["restrictions"]["read_only"])
            self.assertFalse(first["restrictions"]["automatic_live_change"])
            self.assertEqual(second["status"], "SKIPPED")
            self.assertEqual(third["status"], "OK")
            self.assertEqual(pipeline.calls, 2)
            state = json.loads((root / "adaptive_research_state.json").read_text())
            self.assertEqual(state["last_trade_count"], 2)
            self.assertTrue(state["last_trade_hash"])
            self.assertTrue((root / "experiment_promotion_report.json").exists())
            self.assertIn("promotion", state)

    def test_stage_failure_does_not_stop_following_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pipeline = AdaptivePipeline(Path(temporary), executor=RaisingExecutor())
            results = pipeline.run()
        statuses = {result.key: result.status for result in results}
        self.assertEqual(statuses["loss"], "ERROR")
        self.assertEqual(statuses["memory"], "OK")
        self.assertEqual(statuses["orchestrator"], "OK")
        self.assertEqual(len(results), len(STAGE_SPECS))

    def test_scheduler_delegates_incremental_check(self) -> None:
        class Engine:
            def __init__(self) -> None:
                self.force = None

            def run(self, *, force=False):  # type: ignore[no-untyped-def]
                self.force = force
                return {"status": "SKIPPED"}

        engine = Engine()
        scheduler = AdaptiveResearchScheduler(engine, interval_seconds=1)  # type: ignore[arg-type]
        result = scheduler.run_once(force=True)
        self.assertEqual(result["status"], "SKIPPED")
        self.assertTrue(engine.force)

    def test_stage_allowlist_contains_only_research_scripts(self) -> None:
        scripts = {stage.script for stage in STAGE_SPECS}
        self.assertEqual(scripts, {
            "trade_metrics_normalizer.py",
            "trade_loss_analyzer.py",
            "trade_memory.py",
            "shadow_replay.py",
            "strategy_lab/hypothesis_runner.py",
            "research_orchestrator.py",
        })
        prohibited = ("config.py", "multi_timeframe_agent", "portfolio_manager")
        self.assertFalse(any(token in script for script in scripts for token in prohibited))


if __name__ == "__main__":
    unittest.main()
