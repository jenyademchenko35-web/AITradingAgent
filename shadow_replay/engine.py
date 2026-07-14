"""Orchestration layer for execution-aware Shadow Replay v2."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Mapping

from report_metadata import build_report_metadata, timestamp_bounds
from trade_metrics_normalizer import normalize_closed_trades, read_trade_rows

from .execution_model import ExecutionAssumptions, ExecutionModel
from .latency_model import LatencyModel
from .metrics import compare_paths, metric_bundle
from .portfolio import PortfolioAssumptions, ReplayPortfolio
from .report import format_summary, write_csv_atomic, write_json_atomic, write_text_atomic


SCHEMA_VERSION = "2.0"
GENERATOR_VERSION = "2.0"


class ShadowReplayEngine:
    """Build research-only replay reports from canonical closed trades."""

    def __init__(
        self,
        base_dir: Path | str | None = None,
        *,
        execution_assumptions: ExecutionAssumptions | None = None,
        portfolio_assumptions: PortfolioAssumptions | None = None,
    ) -> None:
        self.base_dir = Path(base_dir or Path.cwd()).resolve()
        self.trades_path = self.base_dir / "trades.csv"
        self.report_path = self.base_dir / "shadow_replay_report.json"
        self.summary_path = self.base_dir / "shadow_replay_summary.txt"
        self.csv_path = self.base_dir / "shadow_replay_trades.csv"
        self.execution_model = ExecutionModel(execution_assumptions)
        self.portfolio_assumptions = portfolio_assumptions or PortfolioAssumptions()

    def build_report(self) -> dict[str, Any]:
        """Run all deterministic scenarios without mutating source data."""
        source_rows = read_trade_rows(self.trades_path)
        normalized = normalize_closed_trades(source_rows)
        complete = [
            row for row in normalized if row.get("metrics_status") == "COMPLETE"
        ]
        incomplete = len(normalized) - len(complete)
        warnings: list[str] = []
        if not self.trades_path.exists():
            warnings.append("trades.csv отсутствует")
        if incomplete:
            warnings.append(
                f"{incomplete} закрытых сделок исключены: "
                "R-метрики неполные"
            )

        latency_results: list[dict[str, Any]] = []
        primary_rows: list[dict[str, Any]] = []
        primary_portfolio: dict[str, Any] = {}
        primary_delay = self.execution_model.assumptions.default_latency_seconds
        for seconds in LatencyModel.SCENARIOS:
            execution_rows = [
                self.execution_model.simulate(row, latency_seconds=seconds)
                for row in complete
            ]
            portfolio_rows, portfolio_summary = ReplayPortfolio(
                self.portfolio_assumptions
            ).apply(execution_rows)
            latency_results.append({
                "latency_seconds": seconds,
                "profile": self.execution_model.latency_model.simulate(seconds).to_dict(),
                "effective_execution": metric_bundle(
                    execution_rows, "effective_net_r"
                ),
                "effective_portfolio": metric_bundle(
                    [row for row in portfolio_rows if row.get("portfolio_allowed")],
                    "effective_net_r",
                ),
                "trades_skipped_by_portfolio": portfolio_summary["trades_skipped"],
            })
            if float(seconds) == float(primary_delay):
                primary_rows = portfolio_rows
                primary_portfolio = portfolio_summary

        if not primary_rows and complete:
            execution_rows = [self.execution_model.simulate(row) for row in complete]
            primary_rows, primary_portfolio = ReplayPortfolio(
                self.portfolio_assumptions
            ).apply(execution_rows)

        metrics = compare_paths(primary_rows)
        start, end = timestamp_bounds(normalized, ("opened_at", "closed_at"))
        generated_at = datetime.now(timezone.utc).isoformat()
        execution_quality = self._execution_quality(primary_rows)
        ideal_metrics = metrics["ideal_all"]
        real_metrics = metrics["effective_portfolio"]
        execution_quality.update({
            "average_slippage_bps": round(
                (
                    self.execution_model.assumptions.entry_slippage_bps
                    + self.execution_model.assumptions.exit_slippage_bps
                ) / 2,
                6,
            ),
            "effective_winrate": real_metrics["winrate"],
            "effective_profit_factor": real_metrics["profit_factor"],
            "effective_net_r": real_metrics["net_r"],
        })
        replay_metrics = {
            "ideal_profit_factor": ideal_metrics["profit_factor"],
            "real_profit_factor": real_metrics["profit_factor"],
            "ideal_net_r": ideal_metrics["net_r"],
            "real_net_r": real_metrics["net_r"],
            "fee_impact_r": metrics["impact"]["fee_impact_r"],
            "funding_impact_r": metrics["impact"]["funding_impact_r"],
            "slippage_impact_r": metrics["impact"]["slippage_impact_r"],
            "latency_impact_r": metrics["impact"]["latency_impact_r"],
            "portfolio_impact_r": round(
                real_metrics["net_r"]
                - metrics["effective_execution_all"]["net_r"],
                8,
            ),
        }
        status = "INSUFFICIENT_DATA" if len(complete) < 50 else "OBSERVE_ONLY"
        recommendation = (
            "Статистика пока недостаточна; продолжать "
            "Shadow Research."
            if status == "INSUFFICIENT_DATA"
            else (
                "Сравнивать realistic и ideal metrics, не переносить "
                "модель в LIVE автоматически."
            )
        )
        metadata = build_report_metadata(
            generator="shadow_replay",
            metric_unit="R",
            source_files=(self.trades_path,),
            base_dir=self.base_dir,
            data_period_start=start,
            data_period_end=end,
            closed_trades_total=len(normalized),
            complete_metrics_total=len(complete),
            generated_at=generated_at,
            generator_version=GENERATOR_VERSION,
            schema_version=SCHEMA_VERSION,
        )
        metadata["freshness_ttl_hours"] = 72
        metadata["normalizer_version"] = "1.0"

        return {
            "metadata": metadata,
            "generated_at": generated_at,
            "schema_version": SCHEMA_VERSION,
            "mode": "SHADOW_RESEARCH",
            "status": status,
            "sample": {
                "closed_trades_total": len(normalized),
                "complete_metrics_total": len(complete),
                "incomplete_metrics_total": incomplete,
                "minimum_for_conclusion": 50,
            },
            "assumptions": {
                "execution": self.execution_model.assumptions.to_dict(),
                "portfolio": self.portfolio_assumptions.to_dict(),
                "funding_note": (
                    "Funding оценён пропорционально времени по "
                    "фиксированной research-ставке."
                ),
                "correlation_note": (
                    "Статическая advisory-модель; позиции не блокирует."
                ),
                "correlation": primary_portfolio.get("correlation_model", {}),
            },
            "execution_quality": execution_quality,
            "replay_metrics": replay_metrics,
            "metrics": metrics,
            "latency_scenarios": latency_results,
            "portfolio": primary_portfolio,
            "trades": primary_rows,
            "warnings": warnings,
            "recommendation": recommendation,
            "restrictions": [
                "Не использовать для автоматического изменения LIVE.",
                (
                    "Не изменяет DecisionEngine, Entry/Exit, SL/TP/RR "
                    "или PortfolioManager."
                ),
                (
                    "Параметры исполнения являются прозрачными "
                    "research-assumptions."
                ),
            ],
        }

    def run(self) -> dict[str, Any]:
        """Build and atomically persist JSON, TXT and CSV artifacts."""
        report = self.build_report()
        summary = format_summary(report)
        write_json_atomic(self.report_path, report)
        write_text_atomic(self.summary_path, summary)
        write_csv_atomic(self.csv_path, report.get("trades", []))
        return report

    @staticmethod
    def _execution_quality(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {
                "status": "INSUFFICIENT_DATA",
                "average_fee_r": 0.0,
                "average_funding_r": 0.0,
                "average_slippage_impact_r": 0.0,
                "average_latency_impact_r": 0.0,
                "average_delay_seconds": 0.0,
            }

        def average(key: str) -> float:
            return round(mean(float(row.get(key) or 0) for row in rows), 8)

        average_cost = average("execution_cost_r")
        status = "GOOD" if average_cost <= 0.10 else "WARNING" if average_cost <= 0.25 else "POOR"
        return {
            "status": status,
            "average_fee_r": average("total_fee_r"),
            "average_funding_r": average("funding_r"),
            "average_slippage_impact_r": average("slippage_impact_r"),
            "average_latency_impact_r": average("latency_impact_r"),
            "average_delay_seconds": average("latency_seconds"),
            "average_execution_cost_r": average_cost,
            "poor_fills": sum(row.get("execution_quality") == "POOR" for row in rows),
        }
