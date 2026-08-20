"""Strictly read-only frozen OOS validation for two pre-registered FX hypotheses.

The hypotheses and their boundary are immutable.  Full canonical history is
replayed only to retain entry-time feature context; only trades entered on or
after :data:`FROZEN_OOS_CUTOFF` count as out-of-sample evidence.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .historical_data import HistoricalDataError, load_historical
from .historical_validation import (
    BOOTSTRAP_ITERATIONS,
    BOOTSTRAP_SEED,
    _bootstrap,
    _finite,
    _metric,
    replay,
)

FROZEN_OOS_CUTOFF = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class FrozenHypothesis:
    hypothesis_id: str
    strategy_id: str
    session: str
    side: str | None = None

    def matches(self, outcome: dict[str, Any]) -> bool:
        snapshot = outcome.get("feature_snapshot")
        session = snapshot.get("session") if isinstance(snapshot, dict) else None
        return (
            outcome.get("strategy_id") == self.strategy_id
            and session == self.session
            and (self.side is None or outcome.get("side") == self.side)
        )

    @property
    def description(self) -> str:
        condition = f"session={self.session}"
        if self.side:
            condition = f"side={self.side} and {condition}"
        return f"{self.strategy_id} when {condition}"


FROZEN_HYPOTHESES = (
    FrozenHypothesis("FX-DIAG-V2-01", "FX_RISK_CONSERVATIVE", "LONDON"),
    FrozenHypothesis("FX-DIAG-V2-02", "FX_RISK_CONSERVATIVE", "OVERLAP", "SHORT"),
)


def _parse_timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp is not UTC-aware")
    return parsed.astimezone(UTC)


def _integrity_issues(outcomes: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    for index, outcome in enumerate(outcomes):
        try:
            entry = _parse_timestamp(outcome.get("entry_time"))
            snapshot = outcome.get("feature_snapshot")
            if (
                isinstance(snapshot, dict)
                and snapshot.get("candle_open_at")
                and _parse_timestamp(snapshot["candle_open_at"]) != entry
            ):
                issues.append(f"outcome[{index}] feature snapshot does not match entry time")
            if outcome.get("status") == "CLOSED":
                exit_at = _parse_timestamp(outcome.get("exit_time"))
                if exit_at <= entry:
                    issues.append(f"outcome[{index}] exit time is not after entry time")
                if _finite(outcome.get("pnl_r")) is None:
                    issues.append(f"outcome[{index}] closed pnl_r is non-finite")
        except (TypeError, ValueError):
            issues.append(f"outcome[{index}] has malformed timestamp")
    return issues


def _after_cutoff(outcome: dict[str, Any]) -> bool:
    return _parse_timestamp(outcome["entry_time"]) >= FROZEN_OOS_CUTOFF


def _series_integrity_issues(series: list[Any]) -> list[str]:
    """The loader validates rows; this additionally rejects mixed canonical sources."""
    issues: list[str] = []
    for item in series:
        sources = {candle.source for candle in item.candles}
        if len(sources) != 1:
            issues.append(f"{item.symbol} contains inconsistent candle sources")
        if any(candle.symbol != item.symbol or candle.timeframe != "1h" for candle in item.candles):
            issues.append(f"{item.symbol} contains inconsistent canonical identity")
    return issues


def _oos_metric(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Extend the canonical historical metric with total-candidate count."""
    return {"total_candidate_outcomes": len(rows), "resolved": sum(row.get("status") == "CLOSED" and _finite(row.get("pnl_r")) is not None for row in rows), **_metric(rows)}


def _symbol_metrics(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {symbol: _oos_metric([row for row in rows if row.get("symbol") == symbol]) for symbol in ("EUR/USD", "GBP/USD")}


def _verdict(metrics: dict[str, Any], symbols: dict[str, dict[str, Any]], bootstrap: dict[str, Any]) -> str:
    resolved = int(metrics["resolved"])
    if resolved < 50:
        return "WAITING_FOR_SAMPLE"
    if resolved < 100:
        return "EARLY_SIGNAL"
    pf, expectancy, net_r = metrics["profit_factor"], metrics["expectancy_r"], metrics["net_r"]
    if (pf is not None and float(pf) < 0.95) or (expectancy is not None and float(expectancy) < -0.03) or float(net_r) < -10:
        return "FAIL"
    ci = bootstrap["expectancy_r_ci95"]
    pass_conditions = (
        pf is not None
        and float(pf) >= 1.05
        and expectancy is not None
        and float(expectancy) > 0
        and float(net_r) > 0
        and ci is not None
        and float(ci[0]) >= 0
        and all(int(values["resolved"]) >= 30 for values in symbols.values())
        and all(values["profit_factor"] is not None and float(values["profit_factor"]) >= 0.90 for values in symbols.values())
        and all(values["expectancy_r"] is not None and float(values["expectancy_r"]) >= -0.05 for values in symbols.values())
    )
    return "PASS" if pass_conditions else "INCONCLUSIVE"


def analyze_outcomes(
    outcomes: list[dict[str, Any]],
    *,
    bootstrap_iterations: int = BOOTSTRAP_ITERATIONS,
    bootstrap_seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Validate frozen hypotheses against in-memory replay outcomes only."""
    if bootstrap_iterations <= 0:
        raise ValueError("bootstrap iterations must be positive")
    integrity = _integrity_issues(outcomes)
    if integrity:
        return {
            "cutoff": FROZEN_OOS_CUTOFF.isoformat(),
            "integrity": {"valid": False, "issues": integrity},
            "hypotheses": [{"hypothesis_id": item.hypothesis_id, "verdict": "DATA_INVALID"} for item in FROZEN_HYPOTHESES],
            "comparison": [],
            "overall": "DATA_INVALID",
        }
    oos = [row for row in outcomes if _after_cutoff(row)]
    reports: list[dict[str, Any]] = []
    for hypothesis in FROZEN_HYPOTHESES:
        rows = [row for row in oos if hypothesis.matches(row)]
        metrics = _oos_metric(rows)
        symbols = _symbol_metrics(rows)
        bootstrap = _bootstrap(rows, iterations=bootstrap_iterations, seed=bootstrap_seed)
        reports.append({
            "hypothesis_id": hypothesis.hypothesis_id,
            "description": hypothesis.description,
            "strategy_id": hypothesis.strategy_id,
            "frozen_condition": {"session": hypothesis.session, **({"side": hypothesis.side} if hypothesis.side else {})},
            "metrics": metrics,
            "by_symbol": symbols,
            "bootstrap": bootstrap,
            "verdict": _verdict(metrics, symbols, bootstrap),
        })
    return {
        "asset_class": "FX",
        "mode": "OFFLINE_FROZEN_OOS_VALIDATION_V1",
        "cutoff": FROZEN_OOS_CUTOFF.isoformat(),
        "integrity": {"valid": True, "issues": []},
        "hypotheses": reports,
        "comparison": [
            {
                "hypothesis_id": item["hypothesis_id"],
                "resolved": item["metrics"]["resolved"],
                "profit_factor": item["metrics"]["profit_factor"],
                "expectancy_r": item["metrics"]["expectancy_r"],
                "net_r": item["metrics"]["net_r"],
                "worst_symbol_pf": min(
                    (float(values["profit_factor"]) for values in item["by_symbol"].values() if values["profit_factor"] is not None),
                    default=None,
                ),
                "worst_symbol_expectancy_r": min(
                    (float(values["expectancy_r"]) for values in item["by_symbol"].values() if values["expectancy_r"] is not None),
                    default=None,
                ),
                "verdict": item["verdict"],
            }
            for item in reports
        ],
        "overall": "OOS_EVIDENCE_AVAILABLE" if any(item["metrics"]["total_candidate_outcomes"] for item in reports) else "WAITING_FOR_OOS_DATA",
    }


def build_report(
    *,
    eurusd_path: str | Path,
    gbpusd_path: str | Path,
    bootstrap_iterations: int = BOOTSTRAP_ITERATIONS,
    bootstrap_seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Load explicit canonical inputs and return an in-memory, offline report."""
    try:
        series = [
            load_historical(eurusd_path, symbol="EUR/USD"),
            load_historical(gbpusd_path, symbol="GBP/USD"),
        ]
    except (HistoricalDataError, ValueError) as exc:
        return {
            "asset_class": "FX",
            "mode": "OFFLINE_FROZEN_OOS_VALIDATION_V1",
            "cutoff": FROZEN_OOS_CUTOFF.isoformat(),
            "integrity": {"valid": False, "issues": [str(exc)]},
            "hypotheses": [{"hypothesis_id": item.hypothesis_id, "verdict": "DATA_INVALID"} for item in FROZEN_HYPOTHESES],
            "comparison": [],
            "overall": "DATA_INVALID",
        }
    series_issues = _series_integrity_issues(series)
    if series_issues:
        return {
            "asset_class": "FX",
            "mode": "OFFLINE_FROZEN_OOS_VALIDATION_V1",
            "cutoff": FROZEN_OOS_CUTOFF.isoformat(),
            "integrity": {"valid": False, "issues": series_issues},
            "hypotheses": [{"hypothesis_id": item.hypothesis_id, "verdict": "DATA_INVALID"} for item in FROZEN_HYPOTHESES],
            "comparison": [],
            "overall": "DATA_INVALID",
        }
    outcomes = [outcome for item in series for outcome in replay(item)["outcomes"]]
    report = analyze_outcomes(outcomes, bootstrap_iterations=bootstrap_iterations, bootstrap_seed=bootstrap_seed)
    return {"data_quality": {item.symbol: item.quality.as_dict() for item in series}, **report}


def _print(report: dict[str, Any]) -> None:
    print("FX FROZEN OOS VALIDATION")
    print(f"Cutoff: {report['cutoff']}")
    for item in report["hypotheses"]:
        print(item["hypothesis_id"])
        if "metrics" in item:
            print(f"Resolved: {item['metrics']['resolved']}")
            print(f"PF: {item['metrics']['profit_factor']}")
            print(f"Expectancy: {item['metrics']['expectancy_r']}")
        print(f"Verdict: {item['verdict']}")
    print(f"Overall: {report['overall']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eurusd", type=Path, required=True)
    parser.add_argument("--gbpusd", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    report = build_report(eurusd_path=args.eurusd, gbpusd_path=args.gbpusd)
    _print(report)
    if args.json_output:
        args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
