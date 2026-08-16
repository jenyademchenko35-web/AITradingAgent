"""Deterministic, read-only Research Lab v2 checkpoint report.

The report analyses only canonical CLOSED outcomes and never changes research
state, historical outcomes, strategy parameters, or runtime configuration.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping


CURRENT_ATTRIBUTION_VERSION = "attribution_chain_v1"
CORRELATED_ENTRY_WINDOW_SECONDS = 300
ATTRIBUTION_IDS = (
    "outcome_id", "feature_snapshot_id", "signal_id", "decision_id",
    "strategy_version", "attribution_version",
)
NUMERIC_FEATURES = (
    "adx", "rsi", "atr", "atr_pct", "atr_percentile", "volume_ratio",
    "trend_score", "structure_score", "momentum_score", "risk_score",
    "signal_score",
)


def _utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
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


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _parse_snapshot(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return dict(decoded) if isinstance(decoded, Mapping) else None


def _metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values = [value for row in rows if (value := _number(row.get("pnl_r"))) is not None]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_positive, gross_negative = sum(wins), sum(losses)
    return {
        "trades": len(values), "wins": len(wins), "losses": len(losses),
        "winrate_pct": round(len(wins) / len(values) * 100, 2) if values else None,
        "gross_positive_r": round(gross_positive, 6),
        "gross_negative_r": round(gross_negative, 6),
        "profit_factor": round(gross_positive / abs(gross_negative), 6) if gross_negative else None,
        "net_r": round(sum(values), 6),
        "expectancy_r": round(mean(values), 6) if values else None,
        "average_winner_r": round(mean(wins), 6) if wins else None,
        "average_loser_r": round(mean(losses), 6) if losses else None,
    }


def _group_metrics(rows: Iterable[Mapping[str, Any]], field: str) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        value = str(row.get(field) or "").strip()
        if value:
            groups[value].append(row)
    return [{field: value, **_metrics(group)} for value, group in sorted(groups.items())]


def _feature_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for row in rows:
        snapshot = _parse_snapshot(row.get("feature_snapshot_json"))
        if snapshot is not None:
            enriched.append({**dict(row), "_feature": snapshot})
    return enriched


def _feature_group_metrics(rows: Iterable[Mapping[str, Any]], field: str) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        value = str((row.get("_feature") or {}).get(field) or "").strip()
        if value:
            groups[value].append(row)
    return [{field: value, **_metrics(group)} for value, group in sorted(groups.items())]


def _distribution(values: Iterable[float]) -> dict[str, Any]:
    items = list(values)
    return {
        "count": len(items), "mean": round(mean(items), 6) if items else None,
        "median": round(median(items), 6) if items else None,
        "min": round(min(items), 6) if items else None,
        "max": round(max(items), 6) if items else None,
    }


def _mfe_mae(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("strategy_id") or "UNKNOWN")].append(row)
    output = []
    for strategy, group in sorted(grouped.items()):
        mfe = [value for row in group if (value := _number(row.get("mfe_r"))) is not None]
        mae = [value for row in group if (value := _number(row.get("mae_r"))) is not None]
        losing = [row for row in group if (_number(row.get("pnl_r")) or 0) < 0]
        loss_mfe = [value for row in losing if (value := _number(row.get("mfe_r"))) is not None]
        winners = [row for row in group if (_number(row.get("pnl_r")) or 0) > 0]
        output.append({
            "strategy_id": strategy, "mfe_r": _distribution(mfe), "mae_r": _distribution(mae),
            "losing_trades": len(losing),
            "loser_mfe_reached_pct": {
                str(level): round(sum(value >= level for value in loss_mfe) / len(loss_mfe) * 100, 2)
                if loss_mfe else None for level in (0.5, 1.0, 1.5, 1.9)
            },
            "winner_mfe_r": _distribution(
                value for row in winners if (value := _number(row.get("mfe_r"))) is not None
            ),
            "winner_mae_r": _distribution(
                value for row in winners if (value := _number(row.get("mae_r"))) is not None
            ),
        })
    return output


def _durations(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        entry, exit_ = _utc(row.get("entry_time")), _utc(row.get("exit_time"))
        if entry and exit_ and exit_ >= entry:
            grouped[str(row.get("strategy_id") or "UNKNOWN")].append((exit_ - entry).total_seconds())
    return [{"strategy_id": strategy, "duration_seconds": _distribution(values)}
            for strategy, values in sorted(grouped.items())]


def _feature_comparison(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in NUMERIC_FEATURES:
        winners, losers = [], []
        for row in rows:
            value = _number((row.get("_feature") or {}).get(field))
            pnl = _number(row.get("pnl_r"))
            if value is None or pnl is None:
                continue
            (winners if pnl > 0 else losers if pnl < 0 else []).append(value)
        if winners or losers:
            result[field] = {"winner": _distribution(winners), "loser": _distribution(losers)}
    return result


def _correlated(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: (_utc(row.get("entry_time")) or datetime.min.replace(tzinfo=timezone.utc)))
    correlations = []
    for index, left in enumerate(ordered):
        left_time = _utc(left.get("entry_time"))
        if left_time is None:
            continue
        for right in ordered[index + 1:]:
            right_time = _utc(right.get("entry_time"))
            if right_time is None or (right_time - left_time).total_seconds() > CORRELATED_ENTRY_WINDOW_SECONDS:
                break
            if (left.get("symbol") == right.get("symbol") and left.get("side") == right.get("side")
                    and left.get("strategy_id") != right.get("strategy_id")):
                correlations.append({
                    "left_shadow_trade_id": left.get("shadow_trade_id"),
                    "right_shadow_trade_id": right.get("shadow_trade_id"),
                    "symbol": left.get("symbol"), "side": left.get("side"),
                    "entry_time_delta_seconds": round((right_time - left_time).total_seconds(), 3),
                })
    return correlations


def _integrity(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [str(row.get("shadow_trade_id") or "") for row in rows]
    missing_ids = {
        field: sum(not row.get(field) for row in rows)
        for field in ATTRIBUTION_IDS
    }
    invalid_pnl = sum(_number(row.get("pnl_r")) is None for row in rows)
    missing_feature_evidence = sum(
        not int(row.get("feature_snapshot_available") or 0)
        or not int(row.get("feature_snapshot_valid") or 0)
        or _parse_snapshot(row.get("feature_snapshot_json")) is None
        for row in rows
    )
    return {
        "total_closed": len(rows),
        "resolved": sum(row.get("join_status") == "RESOLVED" for row in rows),
        "complete": sum(row.get("data_quality") == "COMPLETE" for row in rows),
        "unresolved": sum(row.get("join_status") != "RESOLVED" for row in rows),
        "incomplete": sum(row.get("data_quality") != "COMPLETE" for row in rows),
        "duplicate_shadow_trade_id": sum(count - 1 for count in Counter(ids).values() if count > 1),
        "missing_attribution_ids": missing_ids,
        "missing_or_invalid_feature_snapshot": missing_feature_evidence,
        "nonfinite_pnl_r": invalid_pnl,
        "passes": not any((
            sum(row.get("join_status") != "RESOLVED" for row in rows),
            sum(row.get("data_quality") != "COMPLETE" for row in rows),
            sum(count - 1 for count in Counter(ids).values() if count > 1), invalid_pnl,
            sum(missing_ids.values()), missing_feature_evidence,
        )),
    }


def build_checkpoint(*, database_path: str | Path, history_path: str | Path,
                     since: str) -> dict[str, Any]:
    """Build the checkpoint using only canonical rows closed since ``since``."""
    if _utc(since) is None:
        raise ValueError("--since must be an ISO-8601 timestamp with timezone")
    database_path, history_path = Path(database_path), Path(history_path)
    with _ro_connection(database_path) as connection:
        if not _table_exists(connection, "shadow_trade_outcomes"):
            raise ValueError("shadow_trade_outcomes table is not available")
        closed = [dict(row) for row in connection.execute(
            "SELECT * FROM shadow_trade_outcomes WHERE status='CLOSED' AND exit_time >= ? "
            "ORDER BY exit_time, shadow_trade_id", (since,)
        ).fetchall()]

    # History is intentionally not an analysis source.  Reading it only makes
    # the CLI's supplied input explicit and provides a harmless availability diagnostic.
    try:
        with history_path.open(encoding="utf-8", newline="") as handle:
            history_rows = sum(1 for _ in csv.DictReader(handle))
        history_status = "READ_ONLY_AVAILABLE"
    except OSError:
        history_rows, history_status = None, "NOT_AVAILABLE"

    integrity = _integrity(closed)
    eligible = [row for row in closed if row.get("join_status") == "RESOLVED"
                and row.get("data_quality") == "COMPLETE" and _number(row.get("pnl_r")) is not None]
    features = _feature_rows(eligible)
    total = len(eligible)
    verdict = "INSUFFICIENT_SAMPLE" if total < 5 else "EARLY_SIGNAL" if total < 50 else "READY_FOR_NEXT_CHECKPOINT"
    return {
        "since": since,
        "integrity_gate": integrity,
        "history": {"status": history_status, "rows": history_rows},
        "analysis_population": {"eligible_resolved_complete_closed": total,
                                "excluded_by_integrity": len(closed) - total},
        "sample_summary": {"total": _metrics(eligible), "by_strategy": _group_metrics(eligible, "strategy_id")},
        "by_symbol": _group_metrics(eligible, "symbol"),
        "by_strategy_symbol": _group_metrics(eligible, "strategy_id") if not eligible else [
            {"strategy_id": strategy, "symbol": symbol, **_metrics(group)}
            for (strategy, symbol), group in sorted(
                ((key, value) for key, value in _multi_group(eligible, "strategy_id", "symbol").items()),
                key=lambda item: item[0],
            )
        ],
        "by_direction": _group_metrics(eligible, "side"),
        "by_strategy_direction": _strategy_groups(eligible, "side"),
        "by_market_regime": _feature_group_metrics(features, "market_regime"),
        "by_session": _feature_group_metrics(features, "session"),
        "mfe_mae": _mfe_mae(eligible),
        "holding_duration": {
            "basis": "entry_time_to_exit_time_seconds; holding_candles excluded as legacy may be inflated before 84315b6",
            "by_strategy": _durations(eligible),
        },
        "feature_exploration": {
            "feature_evidence_trades": len(features), "numeric_winner_vs_loser": _feature_comparison(features),
        },
        "correlated_observations": {
            "window_seconds": CORRELATED_ENTRY_WINDOW_SECONDS, "pairs": _correlated(eligible),
        },
        "research_verdict": {
            "status": verdict,
            "message": "Descriptive evidence only; no strategy promotion or parameter change is implied.",
            "sample_size_limitations": "Per-strategy samples below 50 are not statistically conclusive.",
        },
    }


def _multi_group(rows: Iterable[Mapping[str, Any]], *fields: str) -> dict[tuple[str, ...], list[Mapping[str, Any]]]:
    grouped: dict[tuple[str, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        values = tuple(str(row.get(field) or "").strip() for field in fields)
        if all(values):
            grouped[values].append(row)
    return grouped


def _strategy_groups(rows: Iterable[Mapping[str, Any]], field: str) -> list[dict[str, Any]]:
    return [
        {"strategy_id": strategy, field: value, **_metrics(group)}
        for (strategy, value), group in sorted(_multi_group(rows, "strategy_id", field).items())
    ]


def _print_report(report: Mapping[str, Any]) -> None:
    gate = report["integrity_gate"]
    print("RL2 RESEARCH CHECKPOINT")
    print(f"Since: {report['since']}")
    print("Integrity: " + ("PASS" if gate["passes"] else "FAIL"))
    print("Closed/Resolved/Complete: " + "/".join(str(gate[key]) for key in ("total_closed", "resolved", "complete")))
    print("Unresolved/Incomplete/Duplicates: " + "/".join(str(gate[key]) for key in ("unresolved", "incomplete", "duplicate_shadow_trade_id")))
    print(f"Analysis population: {report['analysis_population']['eligible_resolved_complete_closed']}")
    for row in report["sample_summary"]["by_strategy"]:
        print("Strategy {strategy_id}: trades={trades} wins={wins} losses={losses} WR={winrate_pct} PF={profit_factor} NetR={net_r} ExpR={expectancy_r}".format(**row))
    print(f"Verdict: {report['research_verdict']['status']}")
    print(report["research_verdict"]["message"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--history", required=True, type=Path)
    parser.add_argument("--since", required=True)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    report = build_checkpoint(database_path=args.db, history_path=args.history, since=args.since)
    _print_report(report)
    if args.json_output:
        args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
