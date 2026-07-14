"""Isolated allowlist pipeline for read-only research generators."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, Sequence


@dataclass(frozen=True)
class StageSpec:
    """Static contract for one safe research stage."""

    key: str
    label: str
    script: str
    report: str
    timeout_seconds: int = 300


STAGE_SPECS = (
    StageSpec("metrics", "Trade Metrics", "trade_metrics_normalizer.py", "trade_metrics_audit.json", 120),
    StageSpec("loss", "Trade Loss Analyzer", "trade_loss_analyzer.py", "trade_loss_report.json", 300),
    StageSpec("memory", "Trade Memory", "trade_memory.py", "trade_memory_report.json", 180),
    StageSpec("replay", "Shadow Replay", "shadow_replay.py", "shadow_replay_report.json", 300),
    StageSpec("lab", "Strategy Lab", "strategy_lab/hypothesis_runner.py", "hypothesis_report.json", 300),
    StageSpec("orchestrator", "Research Orchestrator", "research_orchestrator.py", "research_orchestrator_report.json", 180),
)

STAGE_BY_KEY = {stage.key: stage for stage in STAGE_SPECS}


@dataclass
class StageResult:
    """Serializable execution result for one stage."""

    key: str
    label: str
    status: str
    started_at: str
    finished_at: str
    duration_seconds: float
    report: str
    return_code: int | None = None
    error: str = ""
    output_tail: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class StageExecutor(Protocol):
    """Injectable stage executor used by production and tests."""

    def execute(self, stage: StageSpec, base_dir: Path) -> StageResult:
        """Execute one allowlisted stage."""


class SubprocessStageExecutor:
    """Run each stage in an isolated Python process."""

    def execute(self, stage: StageSpec, base_dir: Path) -> StageResult:
        started = datetime.now(timezone.utc)
        started_clock = time.monotonic()
        script = (base_dir / stage.script).resolve()
        try:
            script.relative_to(base_dir.resolve())
        except ValueError as exc:
            return self._error(stage, started, started_clock, f"unsafe path: {exc}")
        if not script.is_file():
            return self._error(stage, started, started_clock, "script отсутствует")
        try:
            completed = subprocess.run(
                [sys.executable, "-B", str(script)],
                cwd=base_dir,
                capture_output=True,
                text=True,
                timeout=stage.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._error(
                stage,
                started,
                started_clock,
                f"{type(exc).__name__}: {exc}",
            )

        report_path = base_dir / stage.report
        error = ""
        status = "OK"
        if completed.returncode != 0:
            status = "ERROR"
            error = self._compact(completed.stderr or completed.stdout)
        elif not report_path.exists() or report_path.stat().st_size == 0:
            status = "ERROR"
            error = "ожидаемый отчёт не создан"
        output = self._compact(completed.stdout)
        return StageResult(
            key=stage.key,
            label=stage.label,
            status=status,
            started_at=started.isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_seconds=round(time.monotonic() - started_clock, 3),
            report=stage.report,
            return_code=completed.returncode,
            error=error,
            output_tail=output[-1200:],
        )

    @staticmethod
    def _compact(value: str) -> str:
        return " ".join(str(value or "").split())[:2000]

    def _error(
        self,
        stage: StageSpec,
        started: datetime,
        started_clock: float,
        error: str,
    ) -> StageResult:
        return StageResult(
            key=stage.key,
            label=stage.label,
            status="ERROR",
            started_at=started.isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_seconds=round(time.monotonic() - started_clock, 3),
            report=stage.report,
            error=self._compact(error),
        )


class AdaptivePipeline:
    """Run safe research stages sequentially with failure isolation."""

    def __init__(
        self,
        base_dir: Path,
        executor: StageExecutor | None = None,
        stages: Sequence[StageSpec] = STAGE_SPECS,
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.executor = executor or SubprocessStageExecutor()
        self.stages = tuple(stages)

    def run(self, only: str | None = None) -> list[StageResult]:
        """Run selected stages; one error never stops following stages."""
        selected = [stage for stage in self.stages if only in (None, stage.key)]
        if only and not selected:
            raise ValueError(f"Неизвестная стадия: {only}")
        results: list[StageResult] = []
        for stage in selected:
            try:
                result = self.executor.execute(stage, self.base_dir)
            except Exception as exc:  # noqa: BLE001 - isolation is the contract
                now = datetime.now(timezone.utc).isoformat()
                result = StageResult(
                    key=stage.key,
                    label=stage.label,
                    status="ERROR",
                    started_at=now,
                    finished_at=now,
                    duration_seconds=0.0,
                    report=stage.report,
                    error=f"{type(exc).__name__}: {exc}",
                )
            results.append(result)
        return results


def skipped_results(only: str | None = None) -> list[StageResult]:
    """Build explicit SKIPPED results for an unchanged dataset."""
    now = datetime.now(timezone.utc).isoformat()
    return [
        StageResult(
            key=stage.key,
            label=stage.label,
            status="SKIPPED",
            started_at=now,
            finished_at=now,
            duration_seconds=0.0,
            report=stage.report,
            error="закрытые сделки не изменились",
        )
        for stage in STAGE_SPECS
        if only in (None, stage.key)
    ]
