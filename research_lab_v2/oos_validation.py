"""Read-only, pre-registered OOS validation for canonical RL2 outcomes.

The validator never changes strategy/runtime state.  A cutoff is explicit for
analysis: entries before it are frozen in-sample history; entries at/after it
are the only population used for out-of-sample verdicts.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import random
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

from .research_checkpoint import (
    ATTRIBUTION_IDS,
    CORRELATED_ENTRY_WINDOW_SECONDS,
    _correlated,
)

DEFAULT_BOOTSTRAP_ITERATIONS = 5_000
DEFAULT_BOOTSTRAP_SEED = 20_260_814
OVERALL_MIN_TRADES = 20
SUBSET_MIN_TRADES = 15
FROZEN_IS_BENCHMARK: dict[str, dict[str, float | int | str]] = {
    "RISK_CONSERVATIVE": {"trades": 63, "profit_factor": 1.230769, "net_r": 9.0, "expectancy_r": 0.142857},
    "RISK_CONSERVATIVE_LONG": {"trades": 24, "profit_factor": 1.692308, "net_r": 9.0, "expectancy_r": 0.375},
    "RISK_CONSERVATIVE_SHORT": {"trades": 39, "profit_factor": 1.0, "net_r": 0.0, "expectancy_r": 0.0},
    "TREND_CONFIRM": {"trades": 74, "profit_factor": 0.642857, "net_r": -20.0, "expectancy_r": -0.270270},
    "TREND_CONFIRM_LONG": {"trades": 46, "profit_factor": 0.967742, "net_r": -1.0},
    "TREND_CONFIRM_SHORT": {"trades": 28, "profit_factor": 0.24, "net_r": -19.0, "expectancy_r": -0.678571},
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
        candidate = float(value)
    except (TypeError, ValueError):
        return None
    return candidate if math.isfinite(candidate) else None


def _ro_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _parse_snapshot(value: Any) -> dict[str, Any] | None:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values = [value for row in rows if (value := _number(row.get("pnl_r"))) is not None]
    wins, losses = [value for value in values if value > 0], [value for value in values if value < 0]
    return {
        "trades": len(values), "wins": len(wins), "losses": len(losses),
        "winrate_pct": round(100 * len(wins) / len(values), 6) if values else None,
        "gross_positive_r": round(sum(wins), 6), "gross_negative_r": round(sum(losses), 6),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 6) if losses else None,
        "net_r": round(sum(values), 6), "expectancy_r": round(mean(values), 6) if values else None,
        "average_winner_r": round(mean(wins), 6) if wins else None,
        "average_loser_r": round(mean(losses), 6) if losses else None,
    }


def _bootstrap(rows: Iterable[Mapping[str, Any]], *, iterations: int, seed: int) -> dict[str, Any]:
    values = [value for row in rows if (value := _number(row.get("pnl_r"))) is not None]
    if not values:
        return {"seed": seed, "iterations": iterations, "expectancy_r_ci95": None, "winrate_pct_ci95": None}
    # Reproducible descriptive bootstrap, never a security token.
    generator = random.Random(seed)  # nosec B311
    expectancies, winrates = [], []
    for _ in range(iterations):
        sample = [values[generator.randrange(len(values))] for _ in values]
        expectancies.append(sum(sample) / len(sample))
        winrates.append(100 * sum(value > 0 for value in sample) / len(sample))
    expectancies.sort(); winrates.sort()
    lower = int(0.025 * (iterations - 1)); upper = int(0.975 * (iterations - 1))
    return {
        "seed": seed, "iterations": iterations,
        "expectancy_r_ci95": [round(expectancies[lower], 6), round(expectancies[upper], 6)],
        "winrate_pct_ci95": [round(winrates[lower], 6), round(winrates[upper], 6)],
    }


def _group(rows: Iterable[Mapping[str, Any]], field: str, *, feature: bool = False) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        value = (_parse_snapshot(row.get("feature_snapshot_json")) or {}).get(field) if feature else row.get(field)
        if value not in (None, ""):
            groups[str(value)].append(row)
    return [{field: key, **_metrics(value)} for key, value in sorted(groups.items())]


def _mfe_mae(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("strategy_id") or "UNKNOWN")].append(row)
    result = []
    for strategy, group in sorted(groups.items()):
        mfe = [value for row in group if (value := _number(row.get("mfe_r"))) is not None]
        mae = [value for row in group if (value := _number(row.get("mae_r"))) is not None]
        loser_mfe = [_number(row.get("mfe_r")) for row in group if (_number(row.get("pnl_r")) or 0) < 0]
        loser_mfe = [value for value in loser_mfe if value is not None]
        result.append({
            "strategy_id": strategy,
            "mfe_r": {"mean": round(mean(mfe), 6) if mfe else None, "median": round(median(mfe), 6) if mfe else None},
            "mae_r": {"mean": round(mean(mae), 6) if mae else None, "median": round(median(mae), 6) if mae else None},
            "loser_mfe_reached_pct": {str(level): round(100 * sum(value >= level for value in loser_mfe) / len(loser_mfe), 6) if loser_mfe else None for level in (0.5, 1.0, 1.5, 1.9)},
        })
    return result


def _lookup(groups: Iterable[Mapping[str, Any]], **criteria: str) -> dict[str, Any]:
    for row in groups:
        if all(str(row.get(key)) == value for key, value in criteria.items()):
            return dict(row)
    return {**criteria, **_metrics([])}


def _status(*, enough: bool, supported: bool, contradicted: bool) -> str:
    if not enough:
        return "WAITING_FOR_SAMPLE"
    if contradicted:
        return "CONTRADICTED"
    if supported:
        return "SUPPORTED_EARLY"
    return "INCONCLUSIVE"


def _hypotheses(per_strategy: list[Mapping[str, Any]], per_direction: list[Mapping[str, Any]],
                per_regime: list[Mapping[str, Any]], per_session: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rc, tc = _lookup(per_strategy, strategy_id="RISK_CONSERVATIVE"), _lookup(per_strategy, strategy_id="TREND_CONFIRM")
    rc_long, rc_short = _lookup(per_direction, strategy_id="RISK_CONSERVATIVE", side="LONG"), _lookup(per_direction, strategy_id="RISK_CONSERVATIVE", side="SHORT")
    tc_long, tc_short = _lookup(per_direction, strategy_id="TREND_CONFIRM", side="LONG"), _lookup(per_direction, strategy_id="TREND_CONFIRM", side="SHORT")
    enough_rc, enough_tc = rc["trades"] >= OVERALL_MIN_TRADES, tc["trades"] >= OVERALL_MIN_TRADES
    enough_rc_dir = rc_long["trades"] >= SUBSET_MIN_TRADES and rc_short["trades"] >= SUBSET_MIN_TRADES
    enough_tc_dir = tc_long["trades"] >= SUBSET_MIN_TRADES and tc_short["trades"] >= SUBSET_MIN_TRADES
    rc_exp, tc_exp = _number(rc.get("expectancy_r")) or 0.0, _number(tc.get("expectancy_r")) or 0.0
    rc_pf, tc_pf = _number(rc.get("profit_factor")) or 0.0, _number(tc.get("profit_factor")) or 0.0
    def compare(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[bool, bool]:
        left_expectancy, right_expectancy = _number(left.get("expectancy_r")) or 0.0, _number(right.get("expectancy_r")) or 0.0
        left_pf, right_pf = _number(left.get("profit_factor")) or 0.0, _number(right.get("profit_factor")) or 0.0
        return (
            left_expectancy > right_expectancy and left_pf > right_pf,
            left_expectancy < right_expectancy and left_pf < right_pf,
        )
    rc_long_better, rc_long_worse = compare(rc_long, rc_short)
    tc_short_worse, tc_short_better = compare(tc_long, tc_short)
    h5_supported = abs(_number(tc_long.get("expectancy_r")) or 0.0) <= 0.1 and tc_short_worse
    h5_contradicted = (_number(tc_long.get("expectancy_r")) or 0.0) < -0.25 or tc_short_better
    range_group, high_vol = _lookup(per_regime, market_regime="RANGE"), _lookup(per_regime, market_regime="HIGH_VOLATILITY")
    overlap, london, off_hours = (_lookup(per_session, session=item) for item in ("OVERLAP", "LONDON", "OFF_HOURS"))
    range_better, range_worse = compare(range_group, high_vol)
    overlap_better = all(compare(overlap, other)[0] for other in (london, off_hours))
    return [
        {"id": "H1", "statement": "RISK_CONSERVATIVE overall has positive expectancy / PF > 1.", "status": _status(enough=enough_rc, supported=rc_exp > 0 and rc_pf > 1, contradicted=rc_exp < 0 and rc_pf < 1), "in_sample": FROZEN_IS_BENCHMARK["RISK_CONSERVATIVE"], "oos": rc},
        {"id": "H2", "statement": "RISK_CONSERVATIVE LONG outperforms SHORT.", "status": _status(enough=enough_rc_dir, supported=rc_long_better, contradicted=rc_long_worse), "in_sample": {"long": FROZEN_IS_BENCHMARK["RISK_CONSERVATIVE_LONG"], "short": FROZEN_IS_BENCHMARK["RISK_CONSERVATIVE_SHORT"]}, "oos": {"long": rc_long, "short": rc_short}},
        {"id": "H3", "statement": "TREND_CONFIRM overall has negative expectancy / PF < 1.", "status": _status(enough=enough_tc, supported=tc_exp < 0 and tc_pf < 1, contradicted=tc_exp > 0 and tc_pf > 1), "in_sample": FROZEN_IS_BENCHMARK["TREND_CONFIRM"], "oos": tc},
        {"id": "H4", "statement": "TREND_CONFIRM SHORT materially underperforms LONG.", "status": _status(enough=enough_tc_dir, supported=tc_short_worse, contradicted=tc_short_better), "in_sample": {"long": FROZEN_IS_BENCHMARK["TREND_CONFIRM_LONG"], "short": FROZEN_IS_BENCHMARK["TREND_CONFIRM_SHORT"]}, "oos": {"long": tc_long, "short": tc_short}},
        {"id": "H5", "statement": "TREND_CONFIRM LONG is approximately breakeven relative to SHORT.", "status": _status(enough=enough_tc_dir, supported=h5_supported, contradicted=h5_contradicted), "in_sample": {"long": FROZEN_IS_BENCHMARK["TREND_CONFIRM_LONG"], "short": FROZEN_IS_BENCHMARK["TREND_CONFIRM_SHORT"]}, "oos": {"long": tc_long, "short": tc_short}},
        {"id": "H6", "statement": "RANGE appears stronger than HIGH_VOLATILITY (descriptive only).", "status": _status(enough=range_group["trades"] >= SUBSET_MIN_TRADES and high_vol["trades"] >= SUBSET_MIN_TRADES, supported=range_better, contradicted=range_worse), "oos": {"range": range_group, "high_volatility": high_vol}},
        {"id": "H7", "statement": "OVERLAP appears stronger than LONDON / OFF_HOURS (descriptive only).", "status": _status(enough=all(row["trades"] >= SUBSET_MIN_TRADES for row in (overlap, london, off_hours)), supported=overlap_better, contradicted=False), "oos": {"overlap": overlap, "london": london, "off_hours": off_hours}},
    ]


def freeze_cutoff(database_path: str | Path) -> dict[str, Any]:
    """Derive a one-time deterministic boundary from the latest eligible entry."""
    with _ro_connection(Path(database_path)) as connection:
        row = connection.execute("""
            SELECT shadow_trade_id, entry_time, exit_time FROM shadow_trade_outcomes
            WHERE status='CLOSED' AND join_status='RESOLVED' AND data_quality='COMPLETE' AND pnl_r IS NOT NULL
            ORDER BY entry_time DESC, shadow_trade_id DESC LIMIT 1
        """).fetchone()
    if row is None or _utc(row["entry_time"]) is None:
        raise ValueError("no eligible canonical outcome available to freeze cutoff")
    entry = _utc(row["entry_time"])
    if entry is None:
        raise ValueError("latest eligible outcome has an invalid entry_time")
    return {"cutoff": (entry + timedelta(microseconds=1)).isoformat(), "basis": "one microsecond after latest eligible canonical entry_time", "source_shadow_trade_id": row["shadow_trade_id"], "source_entry_time": entry.isoformat(), "source_exit_time": row["exit_time"]}


def _integrity(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [str(row.get("shadow_trade_id") or "") for row in rows]
    return {
        "closed": len(rows), "resolved": sum(row.get("join_status") == "RESOLVED" for row in rows), "complete": sum(row.get("data_quality") == "COMPLETE" for row in rows),
        "unresolved": sum(row.get("join_status") != "RESOLVED" for row in rows), "incomplete": sum(row.get("data_quality") != "COMPLETE" for row in rows),
        "duplicate_shadow_trade_id": sum(count - 1 for count in Counter(ids).values() if count > 1),
        "missing_attribution_ids": {field: sum(not row.get(field) for row in rows) for field in ATTRIBUTION_IDS},
        "invalid_feature_evidence": sum(_parse_snapshot(row.get("feature_snapshot_json")) is None for row in rows),
        "nonfinite_pnl_r": sum(_number(row.get("pnl_r")) is None for row in rows),
    }


def build_oos_report(*, database_path: str | Path, history_path: str | Path, cutoff: str,
                     bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS, bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED) -> dict[str, Any]:
    parsed_cutoff = _utc(cutoff)
    if parsed_cutoff is None:
        raise ValueError("--cutoff must be a timezone-aware ISO-8601 timestamp")
    if bootstrap_iterations <= 0:
        raise ValueError("bootstrap iterations must be positive")
    with _ro_connection(Path(database_path)) as connection:
        rows = [dict(row) for row in connection.execute("SELECT * FROM shadow_trade_outcomes")]
    # Entry-time partition is deliberate: outcomes opened before cutoff remain IS
    # even if their exit arrives later, preventing a future-close leakage.
    oos = [row for row in rows if (entry := _utc(row.get("entry_time"))) is not None and entry >= parsed_cutoff]
    eligible = [row for row in oos if row.get("status") == "CLOSED" and row.get("join_status") == "RESOLVED" and row.get("data_quality") == "COMPLETE" and _number(row.get("pnl_r")) is not None]
    enriched = [{**row, "_feature": _parse_snapshot(row.get("feature_snapshot_json")) or {}} for row in eligible]
    by_strategy = _group(eligible, "strategy_id")
    by_direction = [{"strategy_id": strategy, "side": side, **_metrics(group)} for (strategy, side), group in sorted(_multi_group(eligible, "strategy_id", "side").items())]
    hypotheses = _hypotheses(by_strategy, by_direction, _group(enriched, "market_regime", feature=True), _group(enriched, "session", feature=True))
    primary = [item["status"] for item in hypotheses[:5]]
    if not eligible or all(status == "WAITING_FOR_SAMPLE" for status in primary):
        verdict = "WAITING_FOR_OOS_SAMPLE"
    elif "CONTRADICTED" in primary and "SUPPORTED_EARLY" in primary:
        verdict = "OOS_MIXED"
    elif "CONTRADICTED" in primary:
        verdict = "OOS_REJECTS_IS_HYPOTHESES"
    elif "SUPPORTED_EARLY" in primary:
        verdict = "OOS_SUPPORTS_IS_HYPOTHESES"
    else:
        verdict = "OOS_EARLY_SIGNAL"
    try:
        with Path(history_path).open(encoding="utf-8", newline="") as handle:
            history = {"status": "READ_ONLY_AVAILABLE", "rows": sum(1 for _ in csv.DictReader(handle))}
    except OSError:
        history = {"status": "NOT_AVAILABLE", "rows": None}
    correlated = _correlated(eligible)
    return {
        "cutoff": parsed_cutoff.isoformat(), "partition": {"in_sample": "entry_time < cutoff", "out_of_sample": "entry_time >= cutoff"},
        "frozen_in_sample_benchmark": copy.deepcopy(FROZEN_IS_BENCHMARK), "history": history,
        "oos_integrity": _integrity(oos), "analysis_population": {"eligible_closed_resolved_complete_finite": len(eligible), "excluded": len(oos) - len(eligible)},
        "metrics": {"total": _metrics(eligible), "by_strategy": by_strategy, "by_strategy_direction": by_direction, "by_strategy_symbol": [{"strategy_id": strategy, "symbol": symbol, **_metrics(group)} for (strategy, symbol), group in sorted(_multi_group(eligible, "strategy_id", "symbol").items())], "by_market_regime": _group(enriched, "market_regime", feature=True), "by_session": _group(enriched, "session", feature=True), "bootstrap": _bootstrap(eligible, iterations=bootstrap_iterations, seed=bootstrap_seed), "mfe_mae": _mfe_mae(eligible)},
        "hypotheses": hypotheses, "correlated_observations": {"window_seconds": CORRELATED_ENTRY_WINDOW_SECONDS, "pairs": correlated, "warning": "HIGH_CORRELATION_DESCRIPTIVE_ONLY" if len(correlated) >= 5 else None},
        "oos_verdict": verdict,
    }


def _multi_group(rows: Iterable[Mapping[str, Any]], *fields: str) -> dict[tuple[str, ...], list[Mapping[str, Any]]]:
    groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        key = tuple(str(row.get(field) or "") for field in fields)
        if all(key):
            groups[key].append(row)
    return groups


def _print_report(report: Mapping[str, Any]) -> None:
    print("RL2 OUT-OF-SAMPLE VALIDATION")
    print(f"Cutoff: {report['cutoff']}")
    print(f"Eligible OOS outcomes: {report['analysis_population']['eligible_closed_resolved_complete_finite']}")
    print(f"Verdict: {report['oos_verdict']}")
    for item in report["hypotheses"]:
        print(f"{item['id']}: {item['status']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--history", required=True, type=Path)
    parser.add_argument("--cutoff")
    parser.add_argument("--freeze-cutoff", action="store_true")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--bootstrap-iterations", type=int, default=DEFAULT_BOOTSTRAP_ITERATIONS)
    parser.add_argument("--bootstrap-seed", type=int, default=DEFAULT_BOOTSTRAP_SEED)
    args = parser.parse_args(argv)
    if args.freeze_cutoff:
        report = freeze_cutoff(args.db)
    elif args.cutoff:
        report = build_oos_report(database_path=args.db, history_path=args.history, cutoff=args.cutoff, bootstrap_iterations=args.bootstrap_iterations, bootstrap_seed=args.bootstrap_seed)
    else:
        parser.error("--cutoff is required for OOS analysis; use --freeze-cutoff once to derive a deterministic cutoff")
    _print_report(report) if "oos_verdict" in report else print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json_output:
        args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
