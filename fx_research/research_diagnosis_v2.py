"""Read-only, pre-registered FX hypothesis diagnosis from historical replay.

The module is deliberately descriptive: it reuses immutable entry-time
snapshots from :mod:`historical_validation`, applies TRAIN-frozen buckets from
the first diagnostic layer, and emits at most three hypotheses for a *future*
frozen test.  It cannot enable FX or alter a strategy/runtime artifact.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .diagnostic_analysis import (
    SPLITS,
    TREND_STRENGTH_FIELD,
    VOLATILITY_FIELD,
    _boundaries,
    _bucket,
    _entry_row,
    _finite,
    _metrics,
)
from .historical_data import load_historical
from .historical_validation import STRATEGIES, replay

SAMPLE_GATES = {"TRAIN": 150, "VALIDATION": 60, "HOLDOUT": 60}
SYMBOLS = ("EUR/USD", "GBP/USD")
ALLOWED_GROUPINGS = (
    ("direction",),
    ("session",),
    ("regime",),
    ("volatility_bucket",),
    ("symbol",),
    ("direction", "session"),
    ("direction", "regime"),
)


def _split_metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    materialized = list(rows)
    return {split: _metrics(row for row in materialized if row["split"] == split) for split in SPLITS}


def _gross_loss(rows: Iterable[Mapping[str, Any]]) -> float:
    return abs(sum(value for row in rows if row.get("status") == "CLOSED" and (value := _finite(row.get("pnl_r"))) is not None and value < 0))


def _minimum_sample(metrics: Mapping[str, Mapping[str, Any]]) -> bool:
    return all(int(metrics[split]["resolved_trades"]) >= SAMPLE_GATES[split] for split in SPLITS)


def _safety_floor(metrics: Mapping[str, Mapping[str, Any]]) -> bool:
    return all(
        metrics[split]["profit_factor"] is not None
        and float(metrics[split]["profit_factor"]) >= 0.90
        and metrics[split]["expectancy_r"] is not None
        and float(metrics[split]["expectancy_r"]) >= -0.05
        for split in SPLITS
    )


def _worst(metrics: Mapping[str, Mapping[str, Any]], field: str) -> float | None:
    values = [_finite(metrics[split][field]) for split in SPLITS]
    return min(values) if all(value is not None for value in values) else None


def _robustness(metrics: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Return a transparent, descriptive score; it is not a promotion score.

    Eligible groups receive:
    ``20*min_pf + 100*min_expectancy + .05*min_sample + 10*positive_net_r
    - 10*pf_dispersion - abs(worst_drawdown)/10``.
    Groups below any required split sample receive a score of zero.
    """
    if not _minimum_sample(metrics):
        return {"eligible": False, "score": 0.0, "formula": "0 when any split is below required sample"}
    min_pf = _worst(metrics, "profit_factor")
    min_expectancy = _worst(metrics, "expectancy_r")
    pfs = [float(metrics[split]["profit_factor"]) for split in SPLITS if metrics[split]["profit_factor"] is not None]
    min_sample = min(int(metrics[split]["resolved_trades"]) for split in SPLITS)
    all_positive_net_r = all(float(metrics[split]["net_r"]) >= 0 for split in SPLITS)
    worst_drawdown = min(float(metrics[split]["max_drawdown_r"]) for split in SPLITS)
    score = (
        20 * float(min_pf or 0.0)
        + 100 * float(min_expectancy or 0.0)
        + 0.05 * min_sample
        + (10 if all_positive_net_r else 0)
        - 10 * (max(pfs) - min(pfs))
        - abs(worst_drawdown) / 10
    )
    return {
        "eligible": True,
        "score": round(score, 8),
        "formula": "20*min_pf + 100*min_expectancy + .05*min_sample + 10*all_nonnegative_net_r - 10*pf_dispersion - abs(worst_drawdown)/10",
    }


def _failure_attribution(rows: list[dict[str, Any]], baseline: Mapping[str, Mapping[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(str(row[field]) for field in fields)].append(row)
    report: list[dict[str, Any]] = []
    for key, group in sorted(grouped.items()):
        splits: dict[str, Any] = {}
        for split in SPLITS:
            scoped = [row for row in group if row["split"] == split]
            metrics = _metrics(scoped)
            base = baseline[split]
            base_loss = _gross_loss(row for row in rows if row["split"] == split)
            base_trades = int(base["resolved_trades"])
            splits[split] = {
                **metrics,
                "net_r_contribution": metrics["net_r"],
                "net_r_contribution_pct": round(100 * float(metrics["net_r"]) / float(base["net_r"]), 8) if base["net_r"] else None,
                "loss_contribution_pct": round(100 * _gross_loss(scoped) / base_loss, 8) if base_loss else None,
                "trade_share_pct": round(100 * int(metrics["resolved_trades"]) / base_trades, 8) if base_trades else None,
            }
        report.append({"dimension": " × ".join(fields), "condition": dict(zip(fields, key)), "splits": splits})
    return report


def _cross_symbol_stable(rows: list[dict[str, Any]]) -> bool:
    """Reject an apparent edge supported by only one of the two canonical pairs."""
    for symbol in SYMBOLS:
        symbol_metrics = _split_metrics(row for row in rows if row["symbol"] == symbol)
        if not all(int(symbol_metrics[split]["resolved_trades"]) > 0 for split in SPLITS):
            return False
        if not _safety_floor(symbol_metrics):
            return False
    return True


def _hypothesis_condition(fields: tuple[str, ...], key: tuple[str, ...]) -> str:
    labels = {
        "symbol": "symbol",
        "direction": "direction",
        "session": "session",
        "regime": "regime",
        "volatility_bucket": "volatility",
    }
    return " and ".join(f"{labels[field]}={value}" for field, value in zip(fields, key))


def _candidate_group(
    *,
    strategy: str,
    fields: tuple[str, ...],
    key: tuple[str, ...],
    rows: list[dict[str, Any]],
    baseline: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    metrics = _split_metrics(rows)
    baseline_pf = _worst(baseline, "profit_factor")
    baseline_expectancy = _worst(baseline, "expectancy_r")
    worst_pf = _worst(metrics, "profit_factor")
    worst_expectancy = _worst(metrics, "expectancy_r")
    sample_ok = _minimum_sample(metrics)
    symbol_ok = "symbol" not in fields and _cross_symbol_stable(rows)
    improvements_ok = (
        worst_pf is not None
        and baseline_pf is not None
        and worst_pf >= baseline_pf + 0.05
        and worst_expectancy is not None
        and baseline_expectancy is not None
        and worst_expectancy >= baseline_expectancy + 0.02
    )
    qualifies = sample_ok and _safety_floor(metrics) and improvements_ok and symbol_ok
    return {
        "strategy_id": strategy,
        "dimensions": list(fields),
        "condition": dict(zip(fields, key)),
        "description": f"{strategy} when {_hypothesis_condition(fields, key)}",
        "splits": metrics,
        "robustness": _robustness(metrics),
        "gate_results": {
            "sample": sample_ok,
            "safety_floor": _safety_floor(metrics),
            "improvement": improvements_ok,
            "cross_symbol_stable": symbol_ok,
        },
        "qualifies": qualifies,
    }


def analyze_outcomes(outcomes: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Generate at most three fixed-condition hypotheses from replay outcomes."""
    raw_rows = [_entry_row(outcome) for outcome in outcomes]
    strategy_rows: dict[str, list[dict[str, Any]]] = {}
    bucket_boundaries: dict[str, dict[str, Any]] = {}
    for strategy in STRATEGIES:
        scoped = [row for row in raw_rows if row["strategy_id"] == strategy]
        train = [row for row in scoped if row["split"] == "TRAIN"]
        volatility = _boundaries(row[VOLATILITY_FIELD] for row in train)
        trend_proxy = _boundaries(row[TREND_STRENGTH_FIELD] for row in train)
        strategy_rows[strategy] = [
            {
                **row,
                "volatility_bucket": _bucket(row[VOLATILITY_FIELD], volatility),
                "trend_strength_bucket": _bucket(row[TREND_STRENGTH_FIELD], trend_proxy),
            }
            for row in scoped
        ]
        bucket_boundaries[strategy] = {
            "volatility": {"field": VOLATILITY_FIELD, "source_split": "TRAIN", "boundaries": volatility},
            "momentum_proxy": {
                "field": "RSI_DISTANCE_PROXY",
                "source_snapshot_field": TREND_STRENGTH_FIELD,
                "source_split": "TRAIN",
                "boundaries": trend_proxy,
            },
        }

    baselines = {strategy: _split_metrics(rows) for strategy, rows in strategy_rows.items()}
    candidates: list[dict[str, Any]] = []
    all_groups: list[dict[str, Any]] = []
    attribution: dict[str, list[dict[str, Any]]] = {}
    for strategy, rows in strategy_rows.items():
        attribution[strategy] = [
            item
            for fields in (
                ("direction",),
                ("session",),
                ("regime",),
                ("volatility_bucket",),
                ("trend_strength_bucket",),
                ("symbol",),
            )
            for item in _failure_attribution(rows, baselines[strategy], fields)
        ]
        for fields in ALLOWED_GROUPINGS:
            grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                grouped[tuple(str(row[field]) for field in fields)].append(row)
            for key, group_rows in sorted(grouped.items()):
                group = _candidate_group(
                    strategy=strategy,
                    fields=fields,
                    key=key,
                    rows=group_rows,
                    baseline=baselines[strategy],
                )
                all_groups.append(group)
                if group["qualifies"]:
                    candidates.append(group)

    hypotheses = sorted(
        candidates,
        key=lambda item: (
            -float(item["robustness"]["score"]),
            item["strategy_id"],
            item["description"],
        ),
    )[:3]
    for index, hypothesis in enumerate(hypotheses, start=1):
        hypothesis["hypothesis_id"] = f"FX-DIAG-V2-{index:02d}"
    qualifying_sample = sum(group["gate_results"]["sample"] for group in all_groups)
    passing_improvement = sum(group["gate_results"]["sample"] and group["gate_results"]["improvement"] for group in all_groups)
    return {
        "asset_class": "FX",
        "mode": "OFFLINE_PRE_REGISTERED_HYPOTHESIS_GENERATION_V2",
        "dimensions_evaluated": ["strategy", "symbol", "direction", "session", "regime", "volatility", "RSI_DISTANCE_PROXY"],
        "allowed_interactions": [" × ".join(("strategy",) + fields) for fields in ALLOWED_GROUPINGS],
        "bucket_boundaries": bucket_boundaries,
        "baseline_metrics": baselines,
        "failure_attribution": attribution,
        "groups": all_groups,
        "multiple_testing": {
            "groups_evaluated": len(all_groups),
            "groups_meeting_sample_gates": qualifying_sample,
            "groups_passing_improvement_gates": passing_improvement,
            "hypotheses_emitted": len(hypotheses),
            "warning": "These are exploratory hypotheses and require a new frozen OOS validation. They are not proven edges.",
        },
        "hypotheses": hypotheses,
        "verdict": "HYPOTHESES_READY_FOR_FROZEN_TEST" if hypotheses else "NO_ROBUST_HYPOTHESIS",
    }


def build_report(*, eurusd_path: str | Path, gbpusd_path: str | Path) -> dict[str, Any]:
    """Replay canonical datasets locally and hold all diagnosis state in memory."""
    series = [
        load_historical(eurusd_path, symbol="EUR/USD"),
        load_historical(gbpusd_path, symbol="GBP/USD"),
    ]
    outcomes = [outcome for item in series for outcome in replay(item)["outcomes"]]
    return {
        "data_quality": {item.symbol: item.quality.as_dict() for item in series},
        **analyze_outcomes(outcomes),
    }


def _print(report: Mapping[str, Any]) -> None:
    print("FX RESEARCH DIAGNOSIS v2")
    print("BASELINE FAILURE ATTRIBUTION")
    for strategy, splits in report["baseline_metrics"].items():
        print(f"- {strategy}: " + ", ".join(f"{split} Net R={metrics['net_r']}" for split, metrics in splits.items()))
    print("CANDIDATE HYPOTHESES")
    if report["hypotheses"]:
        for hypothesis in report["hypotheses"]:
            print(f"{hypothesis['hypothesis_id']}. {hypothesis['description']}")
    else:
        print("NONE")
    print(f"VERDICT: {report['verdict']}")


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
