"""Read-only strategy replay engine for AITradingAgent.

The engine simulates how protective rules and Portfolio Manager constraints
would have affected historical closed trades. It reads existing CSV/JSON files
and writes reports only. It does not modify DecisionEngine, config, strategy
weights, multi_timeframe_agent_v3.py, PortfolioManager, or live trade state.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from portfolio_manager import PortfolioManager


BASE_DIR = Path(__file__).resolve().parent

REPORT_PATH = BASE_DIR / "strategy_replay_report.json"
SUMMARY_PATH = BASE_DIR / "strategy_replay_summary.txt"
SCENARIOS_CSV_PATH = BASE_DIR / "strategy_replay_scenarios.csv"
BLOCKED_TRADES_CSV_PATH = BASE_DIR / "strategy_replay_blocked_trades.csv"

MIN_CLOSED_TRADES_FOR_RECOMMENDATION = 30
CONFIDENCE_THRESHOLD = 90.0

SOURCE_FILES = [
    "trades.csv",
    "signals_v3.csv",
    "decision_debug.csv",
    "decision_diagnostics.csv",
    "decision_explanations.csv",
    "portfolio_manager_dry_run.csv",
    "protective_filter_dry_run.csv",
    "sl_quality_protective_dry_run.csv",
    "confidence_sl_quality_d_dry_run.csv",
    "trade_pattern_discovery_report.json",
    "sl_entry_quality_experiment_report.json",
    "confidence_calibration_experiment_report.json",
    "trend_momentum_conflict_v2_report.json",
    "meta_strategy_validation_report.json",
]

Trade = dict[str, Any]
Predicate = Callable[[Trade], bool]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _profit_factor(trades: list[Trade]) -> float:
    gross_profit = sum(max(_safe_float(trade.get("pnl")), 0.0) for trade in trades)
    gross_loss = abs(sum(min(_safe_float(trade.get("pnl")), 0.0) for trade in trades))
    if gross_loss == 0:
        return round(gross_profit, 4) if gross_profit else 0.0
    return round(gross_profit / gross_loss, 4)


def _metrics(trades: list[Trade]) -> dict[str, Any]:
    wins = [trade for trade in trades if str(trade.get("result", "")).upper() == "WIN"]
    losses = [trade for trade in trades if str(trade.get("result", "")).upper() == "LOSS"]
    net_pnl = round(sum(_safe_float(trade.get("pnl")) for trade in trades), 8)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round((len(wins) / len(trades)) * 100, 2) if trades else 0.0,
        "profit_factor": _profit_factor(trades),
        "net_pnl": net_pnl,
    }


def _data_confidence(sample_size: int) -> str:
    if sample_size >= 100:
        return "HIGH"
    if sample_size >= MIN_CLOSED_TRADES_FOR_RECOMMENDATION:
        return "MEDIUM"
    return "LOW"


def _status_for_sample(sample_size: int) -> str:
    return "OK" if sample_size >= MIN_CLOSED_TRADES_FOR_RECOMMENDATION else "INSUFFICIENT_DATA"


class StrategyReplayEngine:
    """Replay protective rules against historical closed trades."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.portfolio_manager = PortfolioManager()
        self.sources = self._source_status()

    def build_report(self) -> dict[str, Any]:
        """Build and save the replay report."""
        trades = self._load_trade_profiles()
        baseline_metrics = _metrics(trades)
        scenarios = [
            self._baseline_scenario(trades, baseline_metrics),
            self._predicate_scenario(
                "trend_momentum_protective_filter",
                "Block SHORT + bullish local 1H trend + Momentum FAIL.",
                trades,
                baseline_metrics,
                self._matches_trend_momentum_filter,
            ),
            self._predicate_scenario(
                "sl_quality_d_filter",
                "Block SHORT + sl_quality=D.",
                trades,
                baseline_metrics,
                self._matches_sl_quality_d_filter,
            ),
            self._predicate_scenario(
                "confidence_sl_quality_d_filter",
                "Block confidence > 90 + sl_quality=D.",
                trades,
                baseline_metrics,
                self._matches_confidence_sl_quality_d_filter,
            ),
            self._portfolio_scenario(trades, baseline_metrics),
            self._predicate_scenario(
                "combined_protective_filters",
                "Block trend/momentum OR SL quality D OR confidence/SL quality D.",
                trades,
                baseline_metrics,
                lambda trade: (
                    self._matches_trend_momentum_filter(trade)
                    or self._matches_sl_quality_d_filter(trade)
                    or self._matches_confidence_sl_quality_d_filter(trade)
                ),
            ),
        ]

        best = self._best_observed_scenario(scenarios)
        report = {
            "generated_at": _utc_now(),
            "status": _status_for_sample(len(trades)),
            "mode": "read-only historical replay",
            "main_question": "Which protective rules reduced LOSS without losing many WIN?",
            "source_files": self.sources,
            "sample": {
                "closed_trades": len(trades),
                "minimum_for_recommendation": MIN_CLOSED_TRADES_FOR_RECOMMENDATION,
                "data_confidence": _data_confidence(len(trades)),
            },
            "scenarios": {scenario["scenario"]: scenario for scenario in scenarios},
            "best_observed_scenario": best,
            "final_recommendation": self._recommendation(len(trades), best),
            "warnings": [
                "Replay is observational and read-only.",
                "No final recommendation is made while closed trades are below 30.",
                "Portfolio scenario is reconstructed from closed trades, not live execution.",
            ],
        }
        self._write_outputs(report, scenarios)
        return report

    def print_report(self) -> None:
        """Print a compact replay summary."""
        report = self.build_report()
        print(self._summary_text(report))

    def _source_status(self) -> dict[str, Any]:
        status = {}
        for filename in SOURCE_FILES:
            path = self.base_dir / filename
            status[filename] = {
                "exists": path.exists(),
                "rows": len(_read_csv(path)) if filename.endswith(".csv") else None,
                "last_modified": (
                    datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
                    if path.exists()
                    else None
                ),
            }
        return status

    def _load_trade_profiles(self) -> list[Trade]:
        trades_rows = [
            row for row in _read_csv(self.base_dir / "trades.csv")
            if str(row.get("result") or row.get("status", "")).upper() in {"WIN", "LOSS"}
        ]
        comparator_rows = _read_csv(self.base_dir / "trade_comparator.csv")
        pattern_report = _read_json(self.base_dir / "trade_pattern_discovery_report.json")
        profiles = pattern_report.get("profiles", [])
        if not isinstance(profiles, list):
            profiles = []

        trades: list[Trade] = []
        for index, row in enumerate(trades_rows):
            profile = profiles[index] if index < len(profiles) and isinstance(profiles[index], dict) else {}
            comparator = comparator_rows[index] if index < len(comparator_rows) else {}
            pnl = row.get("pnl")
            if pnl in (None, ""):
                pnl = profile.get("pnl", comparator.get("pnl", 0.0))
            trade = {
                "index": index,
                "symbol": row.get("symbol", profile.get("symbol", "")),
                "direction": str(row.get("direction", profile.get("direction", ""))).upper(),
                "result": str(row.get("result") or row.get("status") or profile.get("result", "")).upper(),
                "pnl": _safe_float(pnl),
                "opened_at": row.get("opened_at", comparator.get("opened_at", "")),
                "closed_at": row.get("closed_at", comparator.get("closed_at", "")),
                "decision": profile.get("decision", comparator.get("decision", "")),
                "score": _safe_float(profile.get("score", comparator.get("score", 0.0))),
                "confidence": _safe_float(profile.get("confidence", comparator.get("confidence", 0.0))),
                "momentum": str(profile.get("momentum", comparator.get("momentum_status", ""))).upper(),
                "local_trend": str(profile.get("local_trend", "")).upper(),
                "sl_quality": str(profile.get("sl_quality", "UNKNOWN")).upper(),
                "entry_quality": str(profile.get("entry_quality", "UNKNOWN")).upper(),
                "features": profile.get("features", []),
            }
            trades.append(trade)
        return trades

    def _baseline_scenario(
        self,
        trades: list[Trade],
        baseline: dict[str, Any],
    ) -> dict[str, Any]:
        return self._scenario_payload(
            scenario="baseline",
            description="No rules applied.",
            baseline=baseline,
            kept=trades,
            blocked=[],
            blocked_reasons={},
            supporting_sample=None,
        )

    def _predicate_scenario(
        self,
        scenario: str,
        description: str,
        trades: list[Trade],
        baseline: dict[str, Any],
        predicate: Predicate,
    ) -> dict[str, Any]:
        blocked = [trade for trade in trades if predicate(trade)]
        kept = [trade for trade in trades if not predicate(trade)]
        reasons = {
            trade["index"]: description
            for trade in blocked
        }
        supporting_sample = self._supporting_sample_for_scenario(scenario)
        return self._scenario_payload(
            scenario=scenario,
            description=description,
            baseline=baseline,
            kept=kept,
            blocked=blocked,
            blocked_reasons=reasons,
            supporting_sample=supporting_sample,
        )

    def _portfolio_scenario(
        self,
        trades: list[Trade],
        baseline: dict[str, Any],
    ) -> dict[str, Any]:
        active: list[Trade] = []
        kept: list[Trade] = []
        blocked: list[Trade] = []
        reasons: dict[int, str] = {}

        for trade in sorted(trades, key=lambda item: _parse_dt(item.get("opened_at")) or datetime.min.replace(tzinfo=timezone.utc)):
            opened_at = _parse_dt(trade.get("opened_at"))
            if opened_at:
                active = [
                    active_trade for active_trade in active
                    if not self._is_closed_before(active_trade, opened_at)
                ]

            evaluation = self.portfolio_manager.evaluate_new_trade(
                candidate={
                    "symbol": trade.get("symbol"),
                    "direction": trade.get("direction"),
                    "decision": trade.get("decision"),
                    "score": trade.get("score"),
                    "confidence": trade.get("confidence"),
                },
                active_trades=active,
            )
            if evaluation.get("allowed", True):
                kept.append(trade)
                active.append(trade)
            else:
                blocked.append(trade)
                reasons[trade["index"]] = " | ".join(evaluation.get("reasons", []))

        return self._scenario_payload(
            scenario="portfolio_manager_advisory",
            description="Replay PortfolioManager advisory limits.",
            baseline=baseline,
            kept=kept,
            blocked=blocked,
            blocked_reasons=reasons,
            supporting_sample=len(trades),
        )

    def _scenario_payload(
        self,
        scenario: str,
        description: str,
        baseline: dict[str, Any],
        kept: list[Trade],
        blocked: list[Trade],
        blocked_reasons: Mapping[int, str],
        supporting_sample: int | None,
    ) -> dict[str, Any]:
        after = _metrics(kept)
        wins_lost = sum(1 for trade in blocked if trade.get("result") == "WIN")
        losses_blocked = sum(1 for trade in blocked if trade.get("result") == "LOSS")
        safety_score = self._safety_score(baseline, after, losses_blocked, wins_lost)
        return {
            "scenario": scenario,
            "description": description,
            "trades_total": baseline["trades"],
            "trades_kept": len(kept),
            "trades_blocked": len(blocked),
            "wins_kept": after["wins"],
            "losses_blocked": losses_blocked,
            "wins_lost": wins_lost,
            "winrate_before": baseline["winrate"],
            "winrate_after": after["winrate"],
            "profit_factor_before": baseline["profit_factor"],
            "profit_factor_after": after["profit_factor"],
            "net_pnl_before": baseline["net_pnl"],
            "net_pnl_after": after["net_pnl"],
            "net_delta": round(after["net_pnl"] - baseline["net_pnl"], 8),
            "safety_score": safety_score,
            "data_confidence": _data_confidence(baseline["trades"]),
            "supporting_sample": supporting_sample,
            "blocked_trade_ids": [trade["index"] for trade in blocked],
            "blocked_reasons": {
                str(trade["index"]): blocked_reasons.get(trade["index"], description)
                for trade in blocked
            },
        }

    def _matches_trend_momentum_filter(self, trade: Trade) -> bool:
        return (
            trade.get("direction") == "SHORT"
            and trade.get("local_trend") == "BULLISH"
            and trade.get("momentum") == "FAIL"
        )

    def _matches_sl_quality_d_filter(self, trade: Trade) -> bool:
        return trade.get("direction") == "SHORT" and trade.get("sl_quality") == "D"

    def _matches_confidence_sl_quality_d_filter(self, trade: Trade) -> bool:
        return _safe_float(trade.get("confidence")) > CONFIDENCE_THRESHOLD and trade.get("sl_quality") == "D"

    def _supporting_sample_for_scenario(self, scenario: str) -> int | None:
        if scenario == "trend_momentum_protective_filter":
            report = _read_json(self.base_dir / "trend_momentum_conflict_v2_report.json")
            return int(report.get("summary", {}).get("total_candidates") or report.get("candidates") or 0)
        if scenario == "sl_quality_d_filter":
            report = _read_json(self.base_dir / "sl_entry_quality_experiment_report.json")
            return int(report.get("baseline", {}).get("trades") or 0)
        if scenario == "confidence_sl_quality_d_filter":
            report = _read_json(self.base_dir / "confidence_calibration_experiment_report.json")
            return int(report.get("sample", {}).get("closed_trades") or 0)
        if scenario == "combined_protective_filters":
            report = _read_json(self.base_dir / "meta_strategy_validation_report.json")
            return int(report.get("summary", {}).get("total_hypotheses") or 0)
        return None

    @staticmethod
    def _is_closed_before(trade: Trade, timestamp: datetime) -> bool:
        closed_at = _parse_dt(trade.get("closed_at"))
        return bool(closed_at and closed_at <= timestamp)

    @staticmethod
    def _safety_score(
        baseline: dict[str, Any],
        after: dict[str, Any],
        losses_blocked: int,
        wins_lost: int,
    ) -> int:
        pf_delta = after["profit_factor"] - baseline["profit_factor"]
        wr_delta = after["winrate"] - baseline["winrate"]
        net_delta = after["net_pnl"] - baseline["net_pnl"]
        score = 50
        score += losses_blocked * 8
        score -= wins_lost * 20
        score += pf_delta * 20
        score += wr_delta * 0.8
        if net_delta > 0:
            score += 10
        elif net_delta < 0:
            score -= 10
        return max(0, min(100, int(round(score))))

    @staticmethod
    def _best_observed_scenario(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
        candidates = [scenario for scenario in scenarios if scenario["scenario"] != "baseline"]
        if not candidates:
            return {}
        return max(
            candidates,
            key=lambda item: (
                item["safety_score"],
                item["losses_blocked"] - item["wins_lost"],
                item["net_delta"],
            ),
        )

    @staticmethod
    def _recommendation(sample_size: int, best: dict[str, Any]) -> dict[str, Any]:
        if sample_size < MIN_CLOSED_TRADES_FOR_RECOMMENDATION:
            return {
                "status": "INSUFFICIENT_DATA",
                "reason": (
                    f"Only {sample_size} closed trades are available; "
                    f"minimum is {MIN_CLOSED_TRADES_FOR_RECOMMENDATION}."
                ),
                "best_observed_scenario": best.get("scenario"),
                "apply_automatically": False,
            }
        return {
            "status": "READY_FOR_REVIEW",
            "reason": "Sample threshold is met; manual review is still required.",
            "best_observed_scenario": best.get("scenario"),
            "apply_automatically": False,
        }

    def _write_outputs(
        self,
        report: dict[str, Any],
        scenarios: list[dict[str, Any]],
    ) -> None:
        with REPORT_PATH.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
        with SUMMARY_PATH.open("w", encoding="utf-8") as handle:
            handle.write(self._summary_text(report))
        self._write_scenarios_csv(scenarios)
        self._write_blocked_trades_csv(report)

    @staticmethod
    def _write_scenarios_csv(scenarios: list[dict[str, Any]]) -> None:
        fieldnames = [
            "scenario",
            "trades_total",
            "trades_kept",
            "trades_blocked",
            "wins_kept",
            "losses_blocked",
            "wins_lost",
            "winrate_before",
            "winrate_after",
            "profit_factor_before",
            "profit_factor_after",
            "net_pnl_before",
            "net_pnl_after",
            "net_delta",
            "safety_score",
            "data_confidence",
            "supporting_sample",
            "description",
        ]
        with SCENARIOS_CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for scenario in scenarios:
                writer.writerow({field: scenario.get(field, "") for field in fieldnames})

    def _write_blocked_trades_csv(self, report: dict[str, Any]) -> None:
        trades_by_id = {trade["index"]: trade for trade in self._load_trade_profiles()}
        fieldnames = [
            "scenario",
            "trade_id",
            "symbol",
            "direction",
            "result",
            "pnl",
            "decision",
            "score",
            "confidence",
            "sl_quality",
            "local_trend",
            "momentum",
            "reason",
        ]
        with BLOCKED_TRADES_CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for scenario in report["scenarios"].values():
                for trade_id in scenario.get("blocked_trade_ids", []):
                    trade = trades_by_id.get(trade_id, {})
                    writer.writerow({
                        "scenario": scenario["scenario"],
                        "trade_id": trade_id,
                        "symbol": trade.get("symbol", ""),
                        "direction": trade.get("direction", ""),
                        "result": trade.get("result", ""),
                        "pnl": trade.get("pnl", ""),
                        "decision": trade.get("decision", ""),
                        "score": trade.get("score", ""),
                        "confidence": trade.get("confidence", ""),
                        "sl_quality": trade.get("sl_quality", ""),
                        "local_trend": trade.get("local_trend", ""),
                        "momentum": trade.get("momentum", ""),
                        "reason": scenario.get("blocked_reasons", {}).get(str(trade_id), ""),
                    })

    @staticmethod
    def _summary_text(report: dict[str, Any]) -> str:
        sample = report["sample"]
        best = report.get("best_observed_scenario") or {}
        lines = [
            "Strategy Replay Engine",
            "======================",
            f"Generated: {report['generated_at']}",
            f"Status: {report['status']}",
            f"Closed trades: {sample['closed_trades']}",
            f"Data confidence: {sample['data_confidence']}",
            "",
            "Scenarios:",
        ]
        for scenario in report["scenarios"].values():
            lines.append(
                f"- {scenario['scenario']}: blocked={scenario['trades_blocked']}, "
                f"losses_blocked={scenario['losses_blocked']}, wins_lost={scenario['wins_lost']}, "
                f"PF {scenario['profit_factor_before']} -> {scenario['profit_factor_after']}, "
                f"net_delta={scenario['net_delta']}, safety={scenario['safety_score']}"
            )
        lines.extend([
            "",
            "Best observed:",
            (
                f"- {best.get('scenario')} | safety={best.get('safety_score')} | "
                f"losses_blocked={best.get('losses_blocked')} | wins_lost={best.get('wins_lost')}"
            ) if best else "- None",
            "",
            "Final recommendation:",
            f"- Status: {report['final_recommendation']['status']}",
            f"- Reason: {report['final_recommendation']['reason']}",
            f"- Apply automatically: {report['final_recommendation']['apply_automatically']}",
            "",
            "Files:",
            f"- {REPORT_PATH.name}",
            f"- {SUMMARY_PATH.name}",
            f"- {SCENARIOS_CSV_PATH.name}",
            f"- {BLOCKED_TRADES_CSV_PATH.name}",
        ])
        return "\n".join(lines) + "\n"


def main() -> None:
    """CLI entrypoint."""
    engine = StrategyReplayEngine()
    engine.print_report()


if __name__ == "__main__":
    main()
