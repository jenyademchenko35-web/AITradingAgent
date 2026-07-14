"""Adaptive Research Engine orchestration and CLI."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from report_metadata import build_report_metadata

from adaptive_research.artifact_registry import ArtifactRegistry
from adaptive_research.formatter import (
    format_status,
    format_summary,
)
from adaptive_research.pipeline import (
    STAGE_BY_KEY,
    AdaptivePipeline,
    StageResult,
    skipped_results,
)
from adaptive_research.recommendation_engine import AdaptiveRecommendationEngine
from adaptive_research.state import (
    ResearchRunLock,
    TradeFingerprint,
    build_trade_fingerprint,
    load_state,
    read_json,
    utc_now,
    write_json_atomic,
    write_text_atomic,
)


ENGINE_VERSION = "1.0"
REPORT_SCHEMA_VERSION = "1.0"


class AdaptiveResearchEngine:
    """Run the research pipeline only when the closed-trade sample changes."""

    def __init__(
        self,
        base_dir: Path | str | None = None,
        pipeline: AdaptivePipeline | None = None,
    ) -> None:
        self.base_dir = Path(base_dir or Path(__file__).resolve().parents[1]).resolve()
        self.trades_path = self.base_dir / "trades.csv"
        self.state_path = self.base_dir / "adaptive_research_state.json"
        self.report_path = self.base_dir / "adaptive_research_report.json"
        self.summary_path = self.base_dir / "adaptive_research_summary.txt"
        self.lock_path = self.base_dir / ".adaptive_research.lock"
        self.pipeline = pipeline or AdaptivePipeline(self.base_dir)

    def run(
        self,
        *,
        force: bool = False,
        only: str | None = None,
    ) -> dict[str, Any]:
        """Run changed research, persist state and return one adaptive report."""
        if only and only not in STAGE_BY_KEY:
            raise ValueError(f"Неизвестная стадия: {only}")
        with ResearchRunLock(self.lock_path):
            previous_state = load_state(self.state_path)
            start_fingerprint = build_trade_fingerprint(self.trades_path)
            changed = start_fingerprint.changed_from(previous_state)
            should_run = force or bool(only) or changed
            trigger = (
                "REFRESH"
                if force
                else f"ONLY_{only.upper()}"
                if only
                else "NEW_CLOSED_TRADE"
                if changed
                else "UNCHANGED"
            )
            return self._run_pass(
                previous_state=previous_state,
                start_fingerprint=start_fingerprint,
                should_run=should_run,
                trigger=trigger,
                only=only,
            )

    def _run_pass(
        self,
        *,
        previous_state: Mapping[str, Any],
        start_fingerprint: TradeFingerprint,
        should_run: bool,
        trigger: str,
        only: str | None,
    ) -> dict[str, Any]:
        """Execute and persist one pass while the caller owns any run lock."""
        stage_results = (
            self.pipeline.run(only=only)
            if should_run
            else skipped_results(only=only)
        )

        end_fingerprint = build_trade_fingerprint(self.trades_path)
        source_changed_during_run = (
            should_run
            and start_fingerprint.trade_hash != end_fingerprint.trade_hash
        )
        generated_keys = {
            result.key for result in stage_results
            if result.status == "OK" and not source_changed_during_run
        }
        registry = ArtifactRegistry(
            self.base_dir,
            end_fingerprint,
            previous_records=previous_state.get("artifacts", {}),
        )
        records = registry.inspect_all(generated_keys=generated_keys)
        payloads = registry.accepted_payloads()
        recommendation_started = utc_now()
        recommendation_clock = time.monotonic()
        try:
            recommendation = AdaptiveRecommendationEngine().build(
                payloads,
                end_fingerprint.trade_count,
            )
            recommendation_status = "OK" if should_run else "SKIPPED"
            recommendation_error = (
                "" if should_run else "входные артефакты не изменились"
            )
        except Exception as exc:  # noqa: BLE001 - preserve completed stages
            recommendation = {
                "status": "ERROR",
                "global_confidence": 0.0,
                "global_confidence_percent": 0.0,
                "leader": "Нет согласованной гипотезы",
                "primary": "Recommendation Engine временно недоступен.",
                "findings": [],
                "confidence_fusion": [],
                "forbidden_actions": ["Автоматическое применение запрещено."],
            }
            recommendation_status = "ERROR"
            recommendation_error = f"{type(exc).__name__}: {exc}"
        recommendation_finished = utc_now()
        stage_results.append(StageResult(
            key="recommendation",
            label="Adaptive Recommendation",
            status=recommendation_status,
            started_at=recommendation_started,
            finished_at=recommendation_finished,
            duration_seconds=round(time.monotonic() - recommendation_clock, 4),
            report="adaptive_research_report.json",
            return_code=0 if recommendation_status != "ERROR" else 1,
            error=recommendation_error,
        ))
        status = self._pipeline_status(
            stage_results,
            should_run=should_run,
            source_changed=source_changed_during_run,
        )
        generated_at = utc_now()
        report = self._build_report(
            generated_at=generated_at,
            status=status,
            trigger=trigger,
            fingerprint=end_fingerprint.to_dict(),
            stages=stage_results,
            registry=registry,
            recommendation=recommendation,
            source_changed=source_changed_during_run,
        )
        state = self._build_state(
            previous_state=previous_state,
            report=report,
            stage_results=stage_results,
            records=records,
            should_run=should_run,
        )
        write_json_atomic(self.state_path, state)
        write_json_atomic(self.report_path, report)
        write_text_atomic(self.summary_path, format_summary(report))
        return report

    def load_report(self) -> dict[str, Any]:
        """Read the latest report without running research."""
        return read_json(self.report_path)

    def load_state(self) -> dict[str, Any]:
        """Read scheduler state without running research."""
        return load_state(self.state_path)

    def _build_report(
        self,
        *,
        generated_at: str,
        status: str,
        trigger: str,
        fingerprint: Mapping[str, Any],
        stages: Sequence[StageResult],
        registry: ArtifactRegistry,
        recommendation: Mapping[str, Any],
        source_changed: bool,
    ) -> dict[str, Any]:
        accepted_paths = [
            self.base_dir / record.path
            for record in registry.records.values()
            if record.accepted
        ]
        source_files = [self.trades_path, *accepted_paths]
        complete = 0
        data_period_start = ""
        data_period_end = ""
        normalizer_version = ""
        metrics = registry.records.get("metrics")
        if metrics and metrics.accepted:
            metrics_metadata = metrics.payload.get("metadata", {})
            complete = int(metrics_metadata.get("complete_metrics_total") or 0)
            data_period_start = str(metrics_metadata.get("data_period_start") or "")
            data_period_end = str(metrics_metadata.get("data_period_end") or "")
            normalizer_version = str(
                metrics_metadata.get("normalizer_version")
                or metrics_metadata.get("generator_version")
                or ""
            )
        metadata = build_report_metadata(
            generator="adaptive_research.AdaptiveResearchEngine",
            generator_version=ENGINE_VERSION,
            schema_version=REPORT_SCHEMA_VERSION,
            metric_unit="R",
            source_files=source_files,
            base_dir=self.base_dir,
            data_period_start=data_period_start,
            data_period_end=data_period_end,
            closed_trades_total=int(fingerprint.get("trade_count") or 0),
            complete_metrics_total=complete,
            generated_at=generated_at,
        )
        metadata.update({
            "schema": REPORT_SCHEMA_VERSION,
            "freshness": "CURRENT",
            "freshness_ttl_hours": 24,
            "source": "trades.csv",
            "version": ENGINE_VERSION,
            "hash": str(fingerprint.get("trade_hash") or ""),
            "created_at": generated_at,
            "source_trade_hash": str(fingerprint.get("trade_hash") or ""),
            "normalizer_version": normalizer_version,
        })
        return {
            "generated_at": generated_at,
            "metadata": metadata,
            "status": status,
            "mode": "READ_ONLY_SHADOW_RESEARCH",
            "trade_fingerprint": dict(fingerprint),
            "pipeline": {
                "trigger": trigger,
                "incremental": True,
                "source_changed_during_run": source_changed,
                "stages": [stage.to_dict() for stage in stages],
            },
            "artifact_gate": registry.diagnostics(),
            "artifacts": [
                record.to_dict() for record in registry.records.values()
            ],
            "recommendation": dict(recommendation),
            "restrictions": {
                "read_only": True,
                "automatic_live_change": False,
                "decision_engine_unchanged": True,
                "config_unchanged": True,
                "entry_exit_unchanged": True,
                "sl_tp_rr_unchanged": True,
                "portfolio_manager_unchanged": True,
                "trade_execution_disabled": True,
            },
        }

    @staticmethod
    def _build_state(
        *,
        previous_state: Mapping[str, Any],
        report: Mapping[str, Any],
        stage_results: Sequence[StageResult],
        records: Mapping[str, Any],
        should_run: bool,
    ) -> dict[str, Any]:
        fingerprint = report.get("trade_fingerprint", {})
        previous_details = dict(previous_state.get("stage_details", {}))
        for result in stage_results:
            detail = result.to_dict()
            if result.status == "SKIPPED":
                previous = previous_details.get(result.key, {})
                if isinstance(previous, Mapping) and previous.get("last_success_at"):
                    detail["last_success_at"] = previous.get("last_success_at")
            elif result.status == "OK":
                detail["last_success_at"] = result.finished_at
            previous_details[result.key] = detail
        last_run = (
            str(report.get("generated_at"))
            if should_run
            else str(previous_state.get("last_run") or "")
        )
        stage_statuses = dict(previous_state.get("stages", {}))
        stage_statuses.update({result.key: result.status for result in stage_results})
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "last_trade_count": int(fingerprint.get("trade_count") or 0),
            "last_trade_time": str(fingerprint.get("last_trade_time") or ""),
            "last_trade_hash": str(fingerprint.get("trade_hash") or ""),
            "last_csv_hash": str(fingerprint.get("csv_hash") or ""),
            "last_run": last_run,
            "last_check": str(report.get("generated_at") or ""),
            "pipeline_status": str(report.get("status") or ""),
            "stages": stage_statuses,
            "stage_details": previous_details,
            "artifacts": {
                key: record.to_dict() for key, record in records.items()
            },
            "safety": {
                "read_only": True,
                "live_changes_allowed": False,
                "automatic_application": False,
            },
        }

    @staticmethod
    def _pipeline_status(
        stages: Sequence[StageResult],
        *,
        should_run: bool,
        source_changed: bool,
    ) -> str:
        if source_changed:
            return "SOURCE_CHANGED_DURING_RUN"
        if not should_run:
            return "SKIPPED"
        statuses = [stage.status for stage in stages]
        if statuses and all(status == "OK" for status in statuses):
            return "OK"
        if any(status == "OK" for status in statuses):
            return "PARTIAL"
        return "ERROR"


def build_parser() -> argparse.ArgumentParser:
    """Build the documented Adaptive Research CLI."""
    parser = argparse.ArgumentParser(description="Read-only Adaptive Research Engine")
    parser.add_argument("--refresh", action="store_true", help="Принудительно запустить все стадии.")
    parser.add_argument("--status", action="store_true", help="Показать state без запуска исследований.")
    parser.add_argument("--only", choices=tuple(STAGE_BY_KEY), help="Запустить только одну стадию.")
    parser.add_argument("--watch", action="store_true", help="Следить за новыми закрытыми сделками.")
    parser.add_argument("--interval", type=float, default=60.0, help="Интервал scheduler в секундах.")
    return parser


def cli_main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point shared by the root wrapper and tests."""
    args = build_parser().parse_args(argv)
    engine = AdaptiveResearchEngine()
    if args.status:
        print(format_status(engine.load_report(), engine.load_state()))
        return 0
    if args.watch:
        from adaptive_research.scheduler import AdaptiveResearchScheduler

        AdaptiveResearchScheduler(engine, interval_seconds=args.interval).run_forever()
        return 0
    report = engine.run(force=args.refresh, only=args.only)
    print(format_summary(report))
    return 0 if report.get("status") not in {"ERROR", "SOURCE_CHANGED_DURING_RUN"} else 1
