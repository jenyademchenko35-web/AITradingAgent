"""Offline, chronological validation of FX baseline-transfer research logic.

This module reads local historical exports only.  It does not invoke providers,
open a runtime book, create a database, or alter crypto/FX runtime artifacts.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any

from .features import build_feature_snapshot, evaluate_strategy
from .historical_data import HistoricalSeries, load_historical
from .provider import FXCandle

STRATEGIES = ("FX_TREND_CONFIRM", "FX_RISK_CONSERVATIVE")
BOOTSTRAP_ITERATIONS = 5_000
BOOTSTRAP_SEED = 20_260_820


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _metric(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    values = [value for row in rows if row.get("status") == "CLOSED" and (value := _finite(row.get("pnl_r"))) is not None]
    wins, losses = [item for item in values if item > 0], [item for item in values if item < 0]
    cumulative, peak, drawdown = 0.0, 0.0, 0.0
    for value in values:
        cumulative += value; peak = max(peak, cumulative); drawdown = min(drawdown, cumulative - peak)
    mfe = [value for row in rows if (value := _finite(row.get("mfe_r"))) is not None]
    mae = [value for row in rows if (value := _finite(row.get("mae_r"))) is not None]
    durations = [value for row in rows if (value := _finite(row.get("duration_seconds"))) is not None]
    return {
        "trades": len(values), "wins": len(wins), "losses": len(losses),
        "ambiguous": sum(row.get("status") == "AMBIGUOUS_INTRABAR" for row in rows),
        "winrate_pct": round(100 * len(wins) / len(values), 6) if values else None,
        "gross_positive_r": round(sum(wins), 8), "gross_negative_r": round(sum(losses), 8),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 8) if losses else None,
        "net_r": round(sum(values), 8), "expectancy_r": round(mean(values), 8) if values else None,
        "average_winner_r": round(mean(wins), 8) if wins else None,
        "average_loser_r": round(mean(losses), 8) if losses else None,
        "max_drawdown_r": round(drawdown, 8),
        "mfe_r": {"mean": round(mean(mfe), 8) if mfe else None, "median": round(median(mfe), 8) if mfe else None},
        "mae_r": {"mean": round(mean(mae), 8) if mae else None, "median": round(median(mae), 8) if mae else None},
        "duration_seconds": {"mean": round(mean(durations), 8) if durations else None, "median": round(median(durations), 8) if durations else None},
    }


def _bootstrap(rows: Iterable[Mapping[str, Any]], *, iterations: int, seed: int) -> dict[str, Any]:
    values = [value for row in rows if row.get("status") == "CLOSED" and (value := _finite(row.get("pnl_r"))) is not None]
    if not values:
        return {"seed": seed, "iterations": iterations, "expectancy_r_ci95": None, "winrate_pct_ci95": None}
    generator = random.Random(seed)  # nosec B311 - deterministic research bootstrap.
    expectancy, winrate = [], []
    for _ in range(iterations):
        sample = [values[generator.randrange(len(values))] for _ in values]
        expectancy.append(sum(sample) / len(sample))
        winrate.append(100 * sum(item > 0 for item in sample) / len(sample))
    expectancy.sort(); winrate.sort()
    low, high = int((iterations - 1) * .025), int((iterations - 1) * .975)
    return {"seed": seed, "iterations": iterations, "expectancy_r_ci95": [round(expectancy[low], 8), round(expectancy[high], 8)], "winrate_pct_ci95": [round(winrate[low], 8), round(winrate[high], 8)]}


def _split(index: int, total: int) -> str:
    if index < total * .6:
        return "TRAIN"
    if index < total * .8:
        return "VALIDATION"
    return "HOLDOUT"


def _fold(index: int, total: int) -> str:
    return f"FOLD_{min(4, int(index * 4 / total) + 1)}"


def _open_trade(*, strategy_id: str, snapshot: Mapping[str, Any], side: str, split: str, fold: str) -> dict[str, Any]:
    atr, entry = float(snapshot["atr"]), float(snapshot["current_price"])
    stop, target = (entry - atr, entry + 2 * atr) if side == "LONG" else (entry + atr, entry - 2 * atr)
    return {
        "strategy_id": strategy_id, "strategy_version": "fx_baseline_transfer_v1", "symbol": snapshot["symbol"],
        "timeframe": snapshot["timeframe"], "side": side, "entry_time": snapshot["candle_open_at"],
        "entry_price": entry, "stop_loss": stop, "take_profit": target, "split": split, "fold": fold,
        "feature_snapshot": dict(snapshot), "feature_snapshot_id": snapshot["feature_snapshot_id"],
        "mfe_r": 0.0, "mae_r": 0.0, "holding_candles": 0,
    }


def _close(trade: Mapping[str, Any], candle: FXCandle) -> dict[str, Any] | None:
    result = dict(trade)
    entry, sl, tp = float(result["entry_price"]), float(result["stop_loss"]), float(result["take_profit"])
    risk, long = abs(entry - sl), result["side"] == "LONG"
    favorable = (candle.high - entry) / risk if long else (entry - candle.low) / risk
    adverse = (candle.low - entry) / risk if long else (entry - candle.high) / risk
    result["mfe_r"] = max(float(result["mfe_r"]), favorable); result["mae_r"] = min(float(result["mae_r"]), adverse)
    result["holding_candles"] = int(result["holding_candles"]) + 1
    loss, win = (candle.low <= sl if long else candle.high >= sl), (candle.high >= tp if long else candle.low <= tp)
    if not loss and not win:
        return result
    ambiguous = loss and win
    reason = "AMBIGUOUS_INTRABAR" if ambiguous else "STOP_LOSS" if loss else "TAKE_PROFIT"
    exit_price = None if ambiguous else sl if loss else tp
    pnl = None if ambiguous else round((exit_price - entry) / risk if long else (entry - exit_price) / risk, 8)
    entry_at, exit_at = datetime.fromisoformat(str(result["entry_time"])), candle.candle_open_at
    return {**result, "status": "AMBIGUOUS_INTRABAR" if ambiguous else "CLOSED", "exit_reason": reason,
            "exit_price": exit_price, "exit_time": exit_at.isoformat(), "pnl_r": pnl,
            "duration_seconds": (exit_at - entry_at).total_seconds()}


def replay(series: HistoricalSeries) -> dict[str, Any]:
    """Chronological, in-memory replay; an entry cannot inspect its future candle."""
    opens: dict[tuple[str, str], dict[str, Any]] = {}
    decisions: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    history: list[FXCandle] = []
    for index, candle in enumerate(series.candles):
        for key, trade in list(opens.items()):
            updated = _close(trade, candle)
            if updated is None:  # defensive: close always returns a trade state
                continue
            if updated.get("status") in {"CLOSED", "AMBIGUOUS_INTRABAR"}:
                outcomes.append(updated); del opens[key]
            else:
                opens[key] = updated
        history.append(candle)
        snapshot = build_feature_snapshot(candle, history)
        split, fold = _split(index, len(series.candles)), _fold(index, len(series.candles))
        for strategy_id in STRATEGIES:
            evaluation = evaluate_strategy(strategy_id, snapshot)
            decision = {"strategy_id": strategy_id, "symbol": candle.symbol, "timestamp": candle.candle_open_at.isoformat(), "split": split, "fold": fold, **evaluation}
            decisions.append(decision)
            key = strategy_id, candle.symbol
            if evaluation["accepted"] and key not in opens:
                opens[key] = _open_trade(strategy_id=strategy_id, snapshot=snapshot, side=str(evaluation["direction"]), split=split, fold=fold)
    return {"decisions": decisions, "outcomes": outcomes, "open_at_end": list(opens.values())}


def _groups(rows: list[dict[str, Any]], *fields: str) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(str(row.get(field) or "UNKNOWN") for field in fields)].append(row)
    return [{field: value for field, value in zip(fields, key)} | _metric(value) for key, value in sorted(grouped.items())]


def _strategy_partitions(rows: list[dict[str, Any]], field: str, values: tuple[str, ...]) -> list[dict[str, Any]]:
    """Expose empty chronological windows rather than silently omitting them."""
    result = []
    for strategy in STRATEGIES:
        for value in values:
            result.append({"strategy_id": strategy, field: value, **_metric([
                row for row in rows if row.get("strategy_id") == strategy and row.get(field) == value
            ])})
    return result


def _verdict(rows: list[dict[str, Any]], strategy: str) -> str:
    scoped = [row for row in rows if row["strategy_id"] == strategy and row.get("status") == "CLOSED" and _finite(row.get("pnl_r")) is not None]
    all_metrics = _metric(scoped)
    validation, holdout = _metric([row for row in scoped if row["split"] == "VALIDATION"]), _metric([row for row in scoped if row["split"] == "HOLDOUT"])
    folds = [_metric([row for row in scoped if row["fold"] == f"FOLD_{index}"]) for index in range(1, 5)]
    if len(scoped) < 20 or validation["trades"] < 5 or holdout["trades"] < 5:
        return "INSUFFICIENT_DATA"
    profitable = sum((fold["net_r"] or 0) > 0 for fold in folds)
    val_pf, hold_pf = validation["profit_factor"], holdout["profit_factor"]
    if val_pf and hold_pf and val_pf > 1 and hold_pf > 1 and validation["expectancy_r"] > 0 and holdout["expectancy_r"] > 0:
        return "PROMISING_FOR_SHADOW" if profitable >= 3 else "UNSTABLE"
    if (validation["expectancy_r"] or 0) * (holdout["expectancy_r"] or 0) < 0:
        return "UNSTABLE"
    return "WEAK_EVIDENCE" if (all_metrics["expectancy_r"] or 0) > 0 else "NO_EVIDENCE"


def build_report(*, eurusd_path: str | Path, gbpusd_path: str | Path,
                 bootstrap_iterations: int = BOOTSTRAP_ITERATIONS,
                 bootstrap_seed: int = BOOTSTRAP_SEED) -> dict[str, Any]:
    """Read local exports and return an in-memory advisory report only."""
    if bootstrap_iterations <= 0:
        raise ValueError("bootstrap iterations must be positive")
    eurusd, gbpusd = load_historical(eurusd_path, symbol="EUR/USD"), load_historical(gbpusd_path, symbol="GBP/USD")
    replays = {series.symbol: replay(series) for series in (eurusd, gbpusd)}
    outcomes = [row for replay_result in replays.values() for row in replay_result["outcomes"]]
    decisions = [row for replay_result in replays.values() for row in replay_result["decisions"]]
    return {
        "asset_class": "FX", "mode": "OFFLINE_HISTORICAL_VALIDATION", "data_quality": {eurusd.symbol: eurusd.quality.as_dict(), gbpusd.symbol: gbpusd.quality.as_dict()},
        "decisions": {"total": len(decisions), "eligible": sum(bool(row["accepted"]) for row in decisions)},
        "outcomes": {"closed_or_ambiguous": len(outcomes), "open_at_end": sum(len(replay_result["open_at_end"]) for replay_result in replays.values())},
        "metrics": {"full": _metric(outcomes), "by_strategy": _groups(outcomes, "strategy_id"), "by_symbol": _groups(outcomes, "symbol"), "by_direction": _groups(outcomes, "strategy_id", "side"), "by_split": _strategy_partitions(outcomes, "split", ("TRAIN", "VALIDATION", "HOLDOUT")), "by_fold": _strategy_partitions(outcomes, "fold", ("FOLD_1", "FOLD_2", "FOLD_3", "FOLD_4")), "bootstrap": _bootstrap(outcomes, iterations=bootstrap_iterations, seed=bootstrap_seed)},
        "verdict": {strategy: _verdict(outcomes, strategy) for strategy in STRATEGIES},
        "note": "Offline descriptive evidence only; this report cannot enable FX or alter any strategy.",
    }


def _print(report: Mapping[str, Any]) -> None:
    print("FX HISTORICAL VALIDATION")
    print("DATA QUALITY")
    for symbol, quality in report["data_quality"].items():
        print(f"{symbol}: {quality}")
    for strategy, verdict in report["verdict"].items():
        print(f"{strategy}: {verdict}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eurusd", type=Path, required=True)
    parser.add_argument("--gbpusd", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--bootstrap-iterations", type=int, default=BOOTSTRAP_ITERATIONS)
    parser.add_argument("--bootstrap-seed", type=int, default=BOOTSTRAP_SEED)
    args = parser.parse_args(argv)
    report = build_report(eurusd_path=args.eurusd, gbpusd_path=args.gbpusd, bootstrap_iterations=args.bootstrap_iterations, bootstrap_seed=args.bootstrap_seed)
    _print(report)
    if args.json_output:
        args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
