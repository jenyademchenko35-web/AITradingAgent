"""Read-only conditional-edge decomposition for offline FX historical outcomes.

This module reuses the chronological historical replay and only examines its
entry-time immutable feature snapshots. It is descriptive research: it cannot
enable FX, change parameters, or write a runtime artifact unless --json-output
is explicitly requested by the operator.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .historical_data import load_historical
from .historical_validation import STRATEGIES, replay

SPLITS = ("TRAIN", "VALIDATION", "HOLDOUT")
MINIMUM_RESOLVED = {"TRAIN": 100, "VALIDATION": 40, "HOLDOUT": 40}
VOLATILITY_FIELD = "atr_pct"
TREND_STRENGTH_FIELD = "rsi_distance_from_50"
GROUPINGS = (
    ("symbol",),
    ("direction",),
    ("session",),
    ("regime",),
    ("volatility_bucket",),
    ("trend_strength_bucket",),
    ("symbol", "direction"),
    ("session", "direction"),
    ("regime", "direction"),
)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = int(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _boundaries(values: Iterable[float]) -> dict[str, float] | None:
    finite = sorted(value for item in values if (value := _finite(item)) is not None)
    if not finite:
        return None
    return {
        "low_high": round(_quantile(finite, 1 / 3) or 0.0, 10),
        "medium_high": round(_quantile(finite, 2 / 3) or 0.0, 10),
    }


def _bucket(value: Any, boundaries: Mapping[str, float] | None) -> str:
    number = _finite(value)
    if number is None or boundaries is None:
        return "UNAVAILABLE"
    if number <= float(boundaries["low_high"]):
        return "LOW"
    if number <= float(boundaries["medium_high"]):
        return "MEDIUM"
    return "HIGH"


def _entry_row(outcome: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = outcome.get("feature_snapshot")
    if not isinstance(snapshot, Mapping):
        snapshot = {}
    rsi = _finite(snapshot.get("rsi"))
    return {
        "strategy_id": str(outcome.get("strategy_id") or "UNKNOWN"),
        "symbol": str(outcome.get("symbol") or snapshot.get("symbol") or "UNKNOWN"),
        "direction": str(outcome.get("side") or "UNKNOWN"),
        "session": str(snapshot.get("session") or "UNAVAILABLE"),
        "regime": str(snapshot.get("market_regime") or "UNAVAILABLE"),
        "split": str(outcome.get("split") or "UNKNOWN"),
        "entry_time": str(outcome.get("entry_time") or snapshot.get("candle_open_at") or ""),
        "status": str(outcome.get("status") or "UNKNOWN"),
        "pnl_r": outcome.get("pnl_r"),
        VOLATILITY_FIELD: snapshot.get(VOLATILITY_FIELD),
        TREND_STRENGTH_FIELD: abs(rsi - 50.0) if rsi is not None else None,
    }


def _metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: str(row.get("entry_time") or ""))
    resolved = [value for row in ordered if row.get("status") == "CLOSED" and (value := _finite(row.get("pnl_r"))) is not None]
    wins, losses = [value for value in resolved if value > 0], [value for value in resolved if value < 0]
    cumulative = peak = 0.0
    drawdown = 0.0
    for value in resolved:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = min(drawdown, cumulative - peak)
    return {
        "trades": len(ordered),
        "resolved_trades": len(resolved),
        "ambiguous": sum(row.get("status") == "AMBIGUOUS_INTRABAR" for row in ordered),
        "wins": len(wins),
        "losses": len(losses),
        "winrate_pct": round(100 * len(wins) / len(resolved), 8) if resolved else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 8) if losses else None,
        "net_r": round(sum(resolved), 8),
        "expectancy_r": round(sum(resolved) / len(resolved), 8) if resolved else None,
        "max_drawdown_r": round(drawdown, 8),
    }


def _minimum_sample(metrics_by_split: Mapping[str, Mapping[str, Any]]) -> bool:
    return all(int(metrics_by_split[split]["resolved_trades"]) >= MINIMUM_RESOLVED[split] for split in SPLITS)


def _classification(metrics_by_split: Mapping[str, Mapping[str, Any]]) -> str:
    if not _minimum_sample(metrics_by_split):
        return "INSUFFICIENT_SAMPLE"
    pfs = [metrics_by_split[split]["profit_factor"] for split in SPLITS]
    expectancies = [metrics_by_split[split]["expectancy_r"] for split in SPLITS]
    net_rs = [metrics_by_split[split]["net_r"] for split in SPLITS]
    if all(pf is not None and pf > 1.05 for pf in pfs) and all(value is not None and value > 0 for value in expectancies) and all(value is not None and value >= 0 for value in net_rs):
        return "PROMISING_CONDITIONAL_EDGE"
    if all(value is not None and value <= 0 for value in expectancies) and all(value is not None and value <= 0 for value in net_rs):
        return "NEGATIVE"
    expectancy_signs = {value > 0 for value in expectancies if value is not None and value != 0}
    net_signs = {value > 0 for value in net_rs if value is not None and value != 0}
    if len(expectancy_signs) > 1 or len(net_signs) > 1:
        return "UNSTABLE"
    return "CONSISTENT_NEUTRAL"


def _group_label(fields: tuple[str, ...], key: tuple[str, ...]) -> str:
    return " | ".join(f"{field}={value}" for field, value in zip(fields, key))


def _group_rows(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(str(row[field]) for field in fields)].append(row)
    result: list[dict[str, Any]] = []
    for key, group in sorted(grouped.items()):
        by_split = {split: _metrics(row for row in group if row["split"] == split) for split in SPLITS}
        result.append({
            "group_type": " × ".join(("strategy",) + fields),
            "group": {"strategy_id": group[0]["strategy_id"]} | dict(zip(fields, key)),
            "label": _group_label(fields, key),
            "splits": by_split,
            "classification": _classification(by_split),
            "meets_minimum_sample": _minimum_sample(by_split),
        })
    return result


def analyze_outcomes(outcomes: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return deterministic diagnostics from replay outcomes without writing anything."""
    base_rows = [_entry_row(outcome) for outcome in outcomes]
    strategy_boundaries: dict[str, dict[str, Any]] = {}
    decorated: list[dict[str, Any]] = []
    for strategy in STRATEGIES:
        scoped = [row for row in base_rows if row["strategy_id"] == strategy]
        train = [row for row in scoped if row["split"] == "TRAIN"]
        volatility = _boundaries(row[VOLATILITY_FIELD] for row in train)
        trend_strength = _boundaries(row[TREND_STRENGTH_FIELD] for row in train)
        strategy_boundaries[strategy] = {
            "volatility": {"field": VOLATILITY_FIELD, "source_split": "TRAIN", "boundaries": volatility},
            "trend_strength": {"field": TREND_STRENGTH_FIELD, "source_split": "TRAIN", "boundaries": trend_strength},
        }
        for row in scoped:
            decorated.append({
                **row,
                "volatility_bucket": _bucket(row[VOLATILITY_FIELD], volatility),
                "trend_strength_bucket": _bucket(row[TREND_STRENGTH_FIELD], trend_strength),
            })
    groups: list[dict[str, Any]] = []
    for strategy in STRATEGIES:
        scoped = [row for row in decorated if row["strategy_id"] == strategy]
        for fields in GROUPINGS:
            groups.extend(_group_rows(scoped, fields))
    qualifying = [group for group in groups if group["meets_minimum_sample"]]
    promising = [group for group in groups if group["classification"] == "PROMISING_CONDITIONAL_EDGE"]

    def rank_key(group: Mapping[str, Any]) -> tuple[float, float, int, str]:
        splits = group["splits"]
        worst_pf = min(
            float(value) if (value := splits[split]["profit_factor"]) is not None else float("-inf")
            for split in SPLITS
        )
        worst_expectancy = min(
            float(value) if (value := splits[split]["expectancy_r"]) is not None else float("-inf")
            for split in SPLITS
        )
        minimum_resolved = min(int(splits[split]["resolved_trades"]) for split in SPLITS)
        return worst_pf, worst_expectancy, minimum_resolved, str(group["label"])

    shortlist = sorted(qualifying, key=rank_key, reverse=True)[:10]
    if promising:
        verdict = "CONDITIONAL_EDGE_CANDIDATES_FOUND"
    elif any(group["classification"] != "NEGATIVE" for group in qualifying):
        verdict = "WEAK_CONDITIONAL_EVIDENCE"
    else:
        verdict = "NO_CONDITIONAL_EDGE"
    return {
        "asset_class": "FX",
        "mode": "OFFLINE_DIAGNOSTIC_EDGE_DECOMPOSITION_V1",
        "feature_policy": {
            "entry_time_only": True,
            "volatility": VOLATILITY_FIELD,
            "trend_strength": "RSI_DISTANCE_FROM_50_ENTRY_TIME_PROXY (ADX unavailable in baseline snapshot)",
        },
        "train_bucket_boundaries": strategy_boundaries,
        "diagnostic_groups": groups,
        "multiple_testing": {
            "groups_evaluated": len(groups),
            "groups_meeting_minimum_sample": len(qualifying),
            "promising_conditional_edge_groups": len(promising),
            "warning": "Exploratory subgroup discovery only; any candidate requires a new frozen out-of-sample test.",
        },
        "shortlist": shortlist,
        "research_verdict": verdict,
    }


def build_report(*, eurusd_path: str | Path, gbpusd_path: str | Path) -> dict[str, Any]:
    """Replay two canonical CSV datasets and return an in-memory diagnostic report."""
    series = [
        load_historical(eurusd_path, symbol="EUR/USD"),
        load_historical(gbpusd_path, symbol="GBP/USD"),
    ]
    outcomes = [row for item in series for row in replay(item)["outcomes"]]
    return {
        "data_quality": {item.symbol: item.quality.as_dict() for item in series},
        "outcomes": {"total": len(outcomes), "resolved": sum(row.get("status") == "CLOSED" and _finite(row.get("pnl_r")) is not None for row in outcomes)},
        **analyze_outcomes(outcomes),
    }


def _print(report: Mapping[str, Any]) -> None:
    multiple = report["multiple_testing"]
    print("FX DIAGNOSTIC EDGE DECOMPOSITION")
    print(f"Outcomes: {report['outcomes']}")
    print(f"Diagnostic groups: {multiple['groups_evaluated']} | minimum sample: {multiple['groups_meeting_minimum_sample']} | promising: {multiple['promising_conditional_edge_groups']}")
    for group in report["shortlist"]:
        print(f"- {group['group_type']}: {group['label']} -> {group['classification']}")
    print(f"Research verdict: {report['research_verdict']}")


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
