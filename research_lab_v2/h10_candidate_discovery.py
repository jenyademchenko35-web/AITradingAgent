"""Read-only, deterministic H10 candidate-discovery audit for RL2 outcomes.

This is an exploratory report, not an optimizer.  It reads canonical closed
outcomes and their immutable decision-time snapshots, never research runtime
state.  Outcome-side fields such as ``pnl_r``, MFE/MAE and exit reason are used
only to describe associations, never as candidate predictors.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any


MIN_CANDIDATE_N = 20
MAX_CANDIDATES = 5
H9_VERSION = "H9_LIQUIDITY_SWEEP_V1"

# This narrow allow-list is deliberately conservative.  It contains fields
# produced before/at a decision; outcome-side and post-entry values remain out.
SAFE_CATEGORICAL = (
    "strategy_id", "symbol", "timeframe", "side", "market_regime",
    "quality", "live_quality", "decision", "live_decision", "signal",
    "trend", "trend_direction", "momentum_direction", "risk_direction",
    "structure_direction", "session",
)
SAFE_NUMERIC = (
    "signal_score", "confidence", "edge", "atr", "atr_percentile", "adx",
    "volume_ratio", "momentum", "trend_score", "structure_score",
    "momentum_score", "risk_score", "ema_distance", "ema_slope",
    "distance_to_ema200_pct", "volatility", "spread",
)
UNSAFE_FIELDS = {
    "pnl_r", "result_r", "mfe_r", "mae_r", "exit_reason", "exit_time",
    "exit_price", "holding_candles", "status", "outcome_id", "created_at",
    "persisted_at", "current_price", "high", "low",
}


def _utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _snapshot(value: Any) -> dict[str, Any] | None:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _ro_connection(path: Path) -> sqlite3.Connection:
    """Open an audit connection that cannot create, migrate, or write a DB."""
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _max_drawdown(values: list[float]) -> float:
    equity = peak = 0.0
    worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(abs(worst), 6)


def _metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = list(rows)
    values = [value for row in ordered if (value := _number(row.get("pnl_r"))) is not None]
    winners = [value for value in values if value > 0]
    losers = [value for value in values if value < 0]
    gross_positive, gross_negative = sum(winners), sum(losers)
    mfe = [value for row in ordered if (value := _number(row.get("mfe_r"))) is not None]
    mae = [value for row in ordered if (value := _number(row.get("mae_r"))) is not None]
    return {
        "n": len(values), "wins": len(winners), "losses": len(losers),
        "winrate_pct": round(100 * len(winners) / len(values), 6) if values else None,
        "profit_factor": round(gross_positive / abs(gross_negative), 6) if gross_negative else None,
        "net_r": round(sum(values), 6), "expectancy_r": round(mean(values), 6) if values else None,
        "average_mfe_r": round(mean(mfe), 6) if mfe else None,
        "average_mae_r": round(mean(mae), 6) if mae else None,
        "max_drawdown_r": _max_drawdown(values),
    }


def _group(rows: Iterable[Mapping[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        key = tuple(str(row.get(field) or "").strip() for field in fields)
        if all(key):
            groups[key].append(row)
    return [
        {**dict(zip(fields, key)), **_metrics(group_rows), "small_sample": len(group_rows) < MIN_CANDIDATE_N}
        for key, group_rows in sorted(groups.items())
    ]


def _quantile_boundaries(values: list[float]) -> tuple[float, float] | None:
    if len(values) < 3:
        return None
    ordered = sorted(values)
    return (ordered[(len(ordered) - 1) // 3], ordered[2 * (len(ordered) - 1) // 3])


def _numeric_buckets(rows: list[dict[str, Any]], field: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    values = [_number(row["_feature"].get(field)) for row in rows]
    finite = [value for value in values if value is not None]
    boundaries = _quantile_boundaries(finite)
    if boundaries is None:
        return [], None
    low, high = boundaries
    bucketed: list[dict[str, Any]] = []
    for row in rows:
        value = _number(row["_feature"].get(field))
        if value is None:
            continue
        label = "LOW" if value <= low else "HIGH" if value > high else "MEDIUM"
        bucketed.append({**row, f"{field}_bucket": label})
    return _group(bucketed, (f"{field}_bucket",)), {"low_max": low, "medium_max": high, "method": "full_population_descriptive_tertiles_only"}


def _winner_loser(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in SAFE_NUMERIC:
        winners, losers = [], []
        for row in rows:
            value, pnl = _number(row["_feature"].get(field)), _number(row.get("pnl_r"))
            if value is None or pnl is None:
                continue
            (winners if pnl > 0 else losers if pnl < 0 else []).append(value)
        if winners or losers:
            result[field] = {
                "winner_n": len(winners), "winner_mean": round(mean(winners), 6) if winners else None,
                "winner_median": round(median(winners), 6) if winners else None,
                "loser_n": len(losers), "loser_mean": round(mean(losers), 6) if losers else None,
                "loser_median": round(median(losers), 6) if losers else None,
                "mean_difference": round(mean(winners) - mean(losers), 6) if winners and losers else None,
                "coverage_pct": round(100 * (len(winners) + len(losers)) / len(rows), 6) if rows else 0.0,
            }
    return result


def _robustness(rows: list[dict[str, Any]], field: str, condition: str) -> dict[str, Any]:
    selected = [row for row in rows if str(row.get(field) or "") == condition]
    ordered = sorted(selected, key=lambda row: (str(row.get("entry_time") or ""), str(row.get("shadow_trade_id") or "")))
    midpoint = len(ordered) // 2
    major = [row for row in selected if row.get("symbol") in {"BTC/USDT", "ETH/USDT"}]
    remaining = [row for row in selected if row not in major]
    return {
        "chronological_earlier": _metrics(ordered[:midpoint]), "chronological_later": _metrics(ordered[midpoint:]),
        "by_strategy": _group(selected, ("strategy_id",)),
        "major_symbols": _metrics(major), "remaining_symbols": _metrics(remaining),
    }


def _candidate_verdict(candidate: Mapping[str, Any]) -> str:
    metrics = candidate["metrics"]
    robust = candidate["robustness"]
    halves = (robust["chronological_earlier"], robust["chronological_later"])
    stable = all(
        half["n"] >= MIN_CANDIDATE_N // 2
        and (half["expectancy_r"] or 0) > 0
        and (half["profit_factor"] or 0) > 1
        for half in halves
    )
    if metrics["n"] < MIN_CANDIDATE_N:
        return "WEAK"
    return "PROMISING_FOR_FORWARD_TEST" if stable else "INTERESTING"


def _candidates(rows: list[dict[str, Any]], baseline: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: list[tuple[str, str, list[dict[str, Any]]]] = []
    for field in SAFE_CATEGORICAL:
        if field in {"strategy_id", "symbol", "timeframe", "side"}:
            grouped = _group(rows, (field,))
        else:
            grouped = _group([{**row, field: row["_feature"].get(field)} for row in rows], (field,))
        for item in grouped:
            value = str(item.get(field) or "")
            group_rows = [row for row in rows if str((row if field in {"strategy_id", "symbol", "timeframe", "side"} else row["_feature"]).get(field) or "") == value]
            groups.append((field, value, group_rows))
    evaluated: list[dict[str, Any]] = []
    negative: list[dict[str, Any]] = []
    for field, value, group_rows in groups:
        metrics = _metrics(group_rows)
        improves = (metrics["profit_factor"] or 0) > (baseline["profit_factor"] or 0) and (metrics["expectancy_r"] or 0) > (baseline["expectancy_r"] or 0)
        item = {
            "name": f"{field}={value}", "features": [field], "condition": {field: value},
            "metrics": metrics, "baseline": dict(baseline),
            "data_coverage_pct": round(100 * len(group_rows) / len(rows), 6) if rows else 0.0,
            "leakage_risk": "LOW", "overfitting_risk": "HIGH_EXPLORATORY", "interpretability": "HIGH",
            "robustness": _robustness([{**row, field: row["_feature"].get(field)} if field not in {"strategy_id", "symbol", "timeframe", "side"} else row for row in rows], field, value),
        }
        item["verdict"] = _candidate_verdict(item)
        if improves and metrics["n"] >= MIN_CANDIDATE_N:
            evaluated.append(item)
        elif improves:
            negative.append({"name": item["name"], "reason": "INSUFFICIENT_SAMPLE", "n": metrics["n"]})
    def key(item: Mapping[str, Any]) -> tuple[float, float, int, str]:
        metrics = item["metrics"]
        return (-(metrics["profit_factor"] or -1), -(metrics["expectancy_r"] or -999), -metrics["n"], str(item["name"]))
    return sorted(evaluated, key=key)[:MAX_CANDIDATES], sorted(negative, key=lambda item: (item["name"], item["n"]))


def _inventory(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys: set[str] = set()
    for row in rows:
        keys.update(row["_feature"].keys())
    result = []
    for field in sorted(keys | set(SAFE_CATEGORICAL) | set(SAFE_NUMERIC) | UNSAFE_FIELDS):
        coverage = sum(field in row["_feature"] and row["_feature"].get(field) not in (None, "") for row in rows)
        safe = field in SAFE_CATEGORICAL or field in SAFE_NUMERIC
        reason = "persisted decision-time allow-list" if safe else "outcome/post-entry or unapproved field; excluded"
        result.append({"field": field, "source": "shadow_trade_outcomes.feature_snapshot_json", "coverage_pct": round(100 * coverage / len(rows), 6) if rows else 0.0, "type": "numeric" if field in SAFE_NUMERIC else "categorical_or_unknown", "decision_time_safe": safe, "reason": reason})
    return result


def _h9_status(rows: list[dict[str, Any]]) -> dict[str, Any]:
    forward = []
    for row in rows:
        feature = row["_feature"]
        evidence = feature.get("liquidity_sweep") if isinstance(feature.get("liquidity_sweep"), Mapping) else {}
        if feature.get("h9_version") == H9_VERSION and feature.get("h9_observation_scope") == "FORWARD_H9" and evidence.get("evidence_status") == "COMPLETE":
            forward.append(row)
    return {"isolated": True, "complete_forward_h9_n": len(forward), "metrics": _metrics(forward), "message": "H9 is reported separately and never ranked as an H10 candidate."}


def build_h10_discovery(*, database_path: str | Path) -> dict[str, Any]:
    """Build a deterministic, read-only H10 discovery report."""
    with _ro_connection(Path(database_path)) as connection:
        if not _table_exists(connection, "shadow_trade_outcomes"):
            raise ValueError("shadow_trade_outcomes table is not available")
        rows = [dict(row) for row in connection.execute("SELECT * FROM shadow_trade_outcomes ORDER BY exit_time, shadow_trade_id")]
        runs = {}
        if _table_exists(connection, "strategy_runs"):
            runs = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM strategy_runs")}
    closed = [row for row in rows if row.get("status") == "CLOSED"]
    duplicate_counts = Counter(str(row.get("shadow_trade_id") or "") for row in closed)
    exclusion = Counter()
    eligible: list[dict[str, Any]] = []
    for row in closed:
        trade_id = str(row.get("shadow_trade_id") or "")
        feature = _snapshot(row.get("feature_snapshot_json"))
        reasons = []
        if not trade_id:
            reasons.append("missing_shadow_trade_id")
        if duplicate_counts.get(trade_id, 0) > 1:
            reasons.append("duplicate_shadow_trade_id")
        if row.get("join_status") != "RESOLVED":
            reasons.append("unresolved_join")
        if row.get("data_quality") != "COMPLETE":
            reasons.append("incomplete_evidence")
        if _number(row.get("pnl_r")) is None:
            reasons.append("nonfinite_pnl_r")
        if feature is None:
            reasons.append("missing_or_invalid_feature_snapshot")
        for field in ("feature_snapshot_id", "signal_id", "decision_id"):
            if not row.get(field):
                reasons.append(f"missing_{field}")
        if row.get("source_run_id") is None or row.get("source_run_id") not in runs:
            reasons.append("missing_source_strategy_run")
        if reasons:
            exclusion.update(reasons)
            continue
        eligible.append({**row, "_feature": feature})
    baseline = _metrics(eligible)
    categorical = {}
    for field in SAFE_CATEGORICAL:
        source_rows = eligible if field in {"strategy_id", "symbol", "timeframe", "side"} else [{**row, field: row["_feature"].get(field)} for row in eligible]
        categorical[field] = _group(source_rows, (field,))
    numeric = {}
    for field in SAFE_NUMERIC:
        groups, boundaries = _numeric_buckets(eligible, field)
        if boundaries is not None:
            numeric[field] = {"boundaries": boundaries, "groups": groups}
    interactions = {}
    for left, right in (("market_regime", "quality"), ("market_regime", "decision"), ("quality", "decision"), ("strategy_id", "market_regime"), ("strategy_id", "quality")):
        prepared = [{**row, left: row.get(left) if left == "strategy_id" else row["_feature"].get(left), right: row.get(right) if right == "strategy_id" else row["_feature"].get(right)} for row in eligible]
        interactions[f"{left}__x__{right}"] = _group(prepared, (left, right))
    candidates, negatives = _candidates(eligible, baseline)
    # No candidate can be called credible without a robust, sufficiently-sized
    # observational signal.  It remains a request for future validation.
    credible = [item for item in candidates if item["verdict"] == "PROMISING_FOR_FORWARD_TEST"]
    return {
        "read_only": True,
        "data_integrity": {"total_outcomes": len(rows), "closed_outcomes": len(closed), "duplicates": sum(count - 1 for count in duplicate_counts.values() if count > 1), "exclusions": dict(sorted(exclusion.items()))},
        "canonical_population": {"eligible_canonical_outcomes": len(eligible), "excluded_closed_outcomes": len(closed) - len(eligible), "identity_unique": not any(count > 1 for count in duplicate_counts.values())},
        "field_inventory": _inventory(eligible), "baseline_performance": baseline,
        "single_factor_findings": {"categorical": categorical, "numeric_descriptive_tertiles": numeric, "warning": "Numeric tertiles are descriptive only; no threshold is optimized or proposed."},
        "winners_vs_losers": _winner_loser(eligible), "limited_interactions": interactions,
        "top_h10_candidates": candidates, "negative_findings": negatives,
        "multiple_testing": {"single_factor_features_inspected": len(categorical) + len(numeric), "interaction_families_inspected": len(interactions), "warning": "Exploratory multiple-testing risk: no group is validated or ready for LIVE."},
        "h9_forward_status": _h9_status(eligible),
        "final_verdict": "H10_CANDIDATE_FOUND_NEEDS_FORWARD_VALIDATION" if credible else "NO_CREDIBLE_H10_CANDIDATE",
    }


def _print_report(report: Mapping[str, Any]) -> None:
    population, baseline = report["canonical_population"], report["baseline_performance"]
    print("H10 CANDIDATE DISCOVERY (READ-ONLY)")
    print(f"Eligible canonical outcomes: {population['eligible_canonical_outcomes']}")
    print(f"Excluded closed outcomes: {population['excluded_closed_outcomes']}")
    print("Baseline: N={n} PF={profit_factor} NetR={net_r} ExpR={expectancy_r}".format(**baseline))
    print(f"Candidates: {len(report['top_h10_candidates'])}; Verdict: {report['final_verdict']}")
    print("H9 remains isolated: N={complete_forward_h9_n}".format(**report["h9_forward_status"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    report = build_h10_discovery(database_path=args.db)
    _print_report(report)
    if args.json:
        if not args.json.parent.exists():
            raise ValueError("--json parent directory must already exist")
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
