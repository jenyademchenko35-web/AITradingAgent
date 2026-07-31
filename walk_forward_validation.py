"""Read-only walk-forward validation for Candidate Laboratory strategies.

This module never imports or mutates the live DecisionEngine.  It validates the
already persisted, closed shadow trades produced by Candidate Laboratory.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import statistics
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TRAIN_SIZE = 200
DEFAULT_TEST_SIZE = 50
DEFAULT_STEP_SIZE = 50
DEFAULT_MIN_TEST_TRADES = 15
MIN_TRAIN_SIZE = 60
MIN_WINDOWS = 3
BOOTSTRAP_ITERATIONS = 2000
BOOTSTRAP_SEED = 20260726

CONFIG_FILE = BASE_DIR / "candidate_configs.json"
DATA_CANDIDATES = (
    BASE_DIR / "candidate_shadow_trades.csv",
    BASE_DIR / "reports" / "candidate_shadow_trades.csv",
)
REPORT_FILE = BASE_DIR / "walk_forward_report.json"
SUMMARY_FILE = BASE_DIR / "walk_forward_summary.txt"
WINDOWS_FILE = BASE_DIR / "walk_forward_windows.csv"

WINDOW_FIELDS = (
    "window_id", "train_start", "train_end", "test_start", "test_end",
    "strategy", "trades", "wins", "losses", "winrate", "profit_factor",
    "net_r", "average_r", "max_drawdown_r", "profitable", "market_regime",
    "closed_trades", "candidate_better_than_baseline", "candidate_profitable",
)


@dataclass(frozen=True)
class Window:
    window_id: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int


@dataclass(frozen=True)
class TimeWindow:
    window_id: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _canonical_strategy(value: Any) -> str:
    text = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "BASELINE": "LIVE_BASELINE",
        "LIVE": "LIVE_BASELINE",
        "MOMENTUMRELAXED": "MOMENTUM_RELAXED",
    }
    return aliases.get(text, text)


def _display_strategy(value: str) -> str:
    return _canonical_strategy(value).replace("_", " ").title()


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def load_candidate_config(
    candidate: str = "momentum_relaxed", config_path: str | Path = CONFIG_FILE,
) -> tuple[str, dict[str, Any] | None]:
    """Return the exact enabled candidate configuration, without defaults."""
    candidate_id = _canonical_strategy(candidate)
    try:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return candidate_id, None
    config = payload.get(candidate_id) if isinstance(payload, Mapping) else None
    if not isinstance(config, Mapping):
        return candidate_id, None
    return candidate_id, dict(config)


def _first(row: Mapping[str, Any], *names: str) -> Any:
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for name in names:
        value = lowered.get(name)
        if value not in (None, ""):
            return value
    return None


def normalize_trade(row: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Normalize one persisted closed trade or return its skip reason."""
    status = str(_first(row, "status", "result", "trade_status") or "").strip().upper()
    if status in {"OPEN", "ACTIVE", "PENDING", ""}:
        return None, "not_closed"
    if status not in {"WIN", "LOSS", "CLOSED", "PROFIT", "STOPPED", "TP", "SL"}:
        return None, "invalid_status"
    timestamp = _parse_timestamp(_first(
        row, "closed_at", "close_time", "exit_time", "timestamp", "time", "opened_at",
    ))
    if timestamp is None:
        return None, "invalid_timestamp"
    result_r = _number(_first(row, "pnl_r", "result_r", "net_r", "r_result", "rr_result"))
    if result_r is None:
        return None, "missing_result_r"
    strategy = _canonical_strategy(_first(
        row, "candidate_id", "strategy_name", "strategy", "candidate",
    ))
    if not strategy:
        return None, "missing_strategy"
    side = str(_first(row, "direction", "side") or "").strip().upper()
    if side not in {"LONG", "SHORT"}:
        side = "UNKNOWN"
    normalized = {
        "trade_id": str(_first(row, "shadow_trade_id", "trade_id", "id") or "").strip(),
        "timestamp": timestamp.isoformat(),
        "_timestamp": timestamp,
        "symbol": str(_first(row, "symbol", "pair") or "UNKNOWN").strip().upper(),
        "side": side,
        "result_r": result_r,
        "is_win": result_r > 0,
        "strategy": strategy,
        "trend_score": _number(_first(row, "trend_score", "trend")) or 0.0,
        "structure_score": _number(_first(row, "structure_score", "structure")) or 0.0,
        "momentum_score": _number(_first(row, "momentum_score", "momentum")) or 0.0,
        "risk_score": _number(_first(row, "risk_score", "risk")) or 0.0,
        "total_score": _number(_first(row, "total_score", "score")) or 0.0,
        "primary_blocker": str(_first(row, "primary_blocker", "blocker") or "unknown"),
        "market_regime": normalize_regime(_first(row, "market_regime", "regime")),
    }
    return normalized, None


def normalize_regime(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", " ")
    if "bull" in text:
        return "bullish"
    if "bear" in text:
        return "bearish"
    if "rang" in text or "sideway" in text:
        return "ranging"
    if "high" in text and ("vol" in text or "atr" in text):
        return "high volatility"
    if "low" in text and ("vol" in text or "atr" in text):
        return "low volatility"
    return "unknown"


def prepare_trades(rows: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize, deduplicate, and chronologically sort closed trades."""
    prepared: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    seen: set[tuple[Any, ...]] = set()
    raw_strategies: Counter[str] = Counter()
    valid_strategies: Counter[str] = Counter()
    closed_rows = 0
    rows_read = 0
    for row in rows:
        rows_read += 1
        status = str(_first(row, "status", "result", "trade_status") or "").strip().upper()
        if status in {"WIN", "LOSS", "CLOSED", "PROFIT", "STOPPED", "TP", "SL"}:
            closed_rows += 1
        raw_strategy = _canonical_strategy(_first(
            row, "candidate_id", "strategy_name", "strategy", "candidate",
        ))
        if raw_strategy:
            raw_strategies[raw_strategy] += 1
        trade, reason = normalize_trade(row)
        if trade is None:
            reasons[reason or "invalid_row"] += 1
            continue
        key = (("trade_id", trade["trade_id"]) if trade["trade_id"] else (
            "legacy", trade["timestamp"], trade["symbol"], trade["side"],
            trade["strategy"], round(trade["result_r"], 10),
        ))
        if key in seen:
            reasons["duplicate"] += 1
            continue
        seen.add(key)
        prepared.append(trade)
        valid_strategies[trade["strategy"]] += 1
    prepared.sort(key=lambda item: (
        item["_timestamp"], item["symbol"], item["side"], item["strategy"],
    ))
    audit = {
        "rows_read": rows_read,
        "closed_rows": closed_rows,
        "valid_closed_trades": len(prepared),
        "rows_skipped": sum(reasons.values()),
        "skip_reasons": dict(sorted(reasons.items())),
        "rows_by_strategy": dict(sorted(raw_strategies.items())),
        "valid_by_strategy": dict(sorted(valid_strategies.items())),
        "filter_stages": {
            "total_rows": rows_read,
            "closed_status_rows": closed_rows,
            "normalized_valid_rows": len(prepared),
        },
    }
    return prepared, audit


def load_trades(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = Path(path)
    audit: dict[str, Any] = {"data_source": str(source)}
    if not source.exists():
        audit.update(rows_read=0, valid_closed_trades=0, rows_skipped=0,
                     skip_reasons={"file_not_found": 1}, read_error="file_not_found")
        return [], audit
    try:
        source_stat = source.stat()
        audit["source_snapshot"] = {
            "size_bytes": source_stat.st_size,
            "mtime_ns": source_stat.st_mtime_ns,
        }
    except OSError:
        # The normal read-error path below remains authoritative if the source
        # disappears between the existence check and opening it.
        pass
    try:
        with source.open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
    except (OSError, csv.Error, UnicodeError) as exc:
        audit.update(rows_read=0, valid_closed_trades=0, rows_skipped=0,
                     skip_reasons={"csv_read_error": 1}, read_error=str(exc))
        return [], audit
    trades, details = prepare_trades(rows)
    audit.update(details)
    return trades, audit


def find_data_source(paths: Sequence[Path] = DATA_CANDIDATES) -> Path:
    """Choose the first existing Candidate Laboratory trade source."""
    return next((path for path in paths if path.exists()), paths[0])


def build_windows(
    sample_size: int, train_size: int = DEFAULT_TRAIN_SIZE,
    test_size: int = DEFAULT_TEST_SIZE, step_size: int = DEFAULT_STEP_SIZE,
    min_test_trades: int = DEFAULT_MIN_TEST_TRADES,
) -> tuple[list[Window], dict[str, int]]:
    """Build rolling chronological windows, shrinking only before evaluation."""
    train_size = max(MIN_TRAIN_SIZE, int(train_size))
    test_size = max(min_test_trades, int(test_size))
    step_size = max(1, int(step_size))

    def generate(train: int, test: int, step: int) -> list[Window]:
        result = []
        start = 0
        while start + train + test <= sample_size:
            result.append(Window(
                len(result) + 1, start, start + train,
                start + train, start + train + test,
            ))
            start += step
        return result

    windows = generate(train_size, test_size, step_size)
    if len(windows) < MIN_WINDOWS and sample_size >= MIN_TRAIN_SIZE + MIN_WINDOWS * min_test_trades:
        test_size = min(test_size, max(min_test_trades, (sample_size - MIN_TRAIN_SIZE) // MIN_WINDOWS))
        step_size = test_size
        train_size = sample_size - (MIN_WINDOWS * test_size)
        train_size = max(MIN_TRAIN_SIZE, train_size)
        windows = generate(train_size, test_size, step_size)
    return windows, {
        "train_size": train_size, "test_size": test_size, "step_size": step_size,
        "min_test_trades": min_test_trades,
    }


def build_time_windows(
    baseline: Sequence[Mapping[str, Any]], candidate: Sequence[Mapping[str, Any]],
    train_size: int = DEFAULT_TRAIN_SIZE, test_size: int = DEFAULT_TEST_SIZE,
    step_size: int = DEFAULT_STEP_SIZE, min_test_trades: int = DEFAULT_MIN_TEST_TRADES,
) -> tuple[list[TimeWindow], dict[str, int]]:
    """Build expanding, non-overlapping OOS periods shared by both strategies."""
    minimum_required = MIN_TRAIN_SIZE + MIN_WINDOWS * min_test_trades
    sample_size = min(len(baseline), len(candidate))
    resolved_train = max(MIN_TRAIN_SIZE, int(train_size))
    resolved_test = max(min_test_trades, int(test_size))
    if sample_size < resolved_train + MIN_WINDOWS * resolved_test:
        resolved_train = MIN_TRAIN_SIZE
        resolved_test = min_test_trades

    timestamps = sorted({
        row["_timestamp"] for row in [*baseline, *candidate]
    })
    windows: list[TimeWindow] = []
    if sample_size < minimum_required or not timestamps:
        return windows, {
            "train_size": resolved_train, "test_size": resolved_test,
            "step_size": resolved_test, "min_test_trades": min_test_trades,
            "minimum_required_per_strategy": minimum_required,
            "possible_windows": 0,
        }

    def count_before(rows: Sequence[Mapping[str, Any]], boundary: datetime) -> int:
        return sum(row["_timestamp"] < boundary for row in rows)

    boundary_index = next((
        index for index, stamp in enumerate(timestamps)
        if count_before(baseline, stamp) >= resolved_train
        and count_before(candidate, stamp) >= resolved_train
    ), None)
    if boundary_index is None:
        return windows, {
            "train_size": resolved_train, "test_size": resolved_test,
            "step_size": resolved_test, "min_test_trades": min_test_trades,
            "minimum_required_per_strategy": minimum_required,
            "possible_windows": 0,
        }

    train_start = timestamps[0]
    test_start = timestamps[boundary_index]
    while len(windows) < MIN_WINDOWS:
        endpoints = timestamps[boundary_index + 1:] + [timestamps[-1] + timedelta(microseconds=1)]
        test_end = next((
            stamp for stamp in endpoints
            if sum(test_start <= row["_timestamp"] < stamp for row in baseline) >= resolved_test
            and sum(test_start <= row["_timestamp"] < stamp for row in candidate) >= resolved_test
        ), None)
        if test_end is None:
            break
        windows.append(TimeWindow(
            len(windows) + 1, train_start, test_start, test_start, test_end,
        ))
        if len(windows) >= MIN_WINDOWS or test_end not in timestamps:
            break
        boundary_index = timestamps.index(test_end)
        test_start = test_end
    return windows, {
        "train_size": resolved_train, "test_size": resolved_test,
        "step_size": resolved_test, "min_test_trades": min_test_trades,
        "minimum_required_per_strategy": minimum_required,
        "possible_windows": len(windows),
    }


def profit_factor(values: Iterable[float]) -> float | None:
    numbers = list(values)
    gross_profit = sum(value for value in numbers if value > 0)
    gross_loss = abs(sum(value for value in numbers if value < 0))
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else None
    return gross_profit / gross_loss


def max_drawdown(values: Iterable[float]) -> float:
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def max_losing_streak(values: Iterable[float]) -> int:
    current = maximum = 0
    for value in values:
        current = current + 1 if value <= 0 else 0
        maximum = max(maximum, current)
    return maximum


def calculate_metrics(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = [float(row["result_r"]) for row in trades]
    wins = sum(value > 0 for value in values)
    losses = len(values) - wins
    pf = profit_factor(values)
    return {
        "trades": len(values),
        "wins": wins,
        "losses": losses,
        "winrate": wins / len(values) * 100 if values else 0.0,
        "gross_profit_r": sum(value for value in values if value > 0),
        "gross_loss_r": abs(sum(value for value in values if value < 0)),
        "profit_factor": pf,
        "net_r": sum(values),
        "average_r": statistics.mean(values) if values else 0.0,
        "median_r": statistics.median(values) if values else 0.0,
        "max_drawdown_r": max_drawdown(values),
        "max_losing_streak": max_losing_streak(values),
    }


def _finite_pf(value: Any) -> float:
    number = _number(value)
    return number if number is not None else (math.inf if value == math.inf else 0.0)


def _json_value(value: Any) -> Any:
    if isinstance(value, float):
        if math.isinf(value):
            return "INF"
        if math.isnan(value):
            return None
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _dominant_regime(trades: Sequence[Mapping[str, Any]]) -> str:
    regimes = Counter(str(row.get("market_regime") or "unknown") for row in trades)
    return regimes.most_common(1)[0][0] if regimes else "unknown"


def evaluate_windows(
    baseline: Sequence[Mapping[str, Any]], candidate: Sequence[Mapping[str, Any]],
    windows: Sequence[Window | TimeWindow],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    baseline_oos: list[dict[str, Any]] = []
    candidate_oos: list[dict[str, Any]] = []
    for window in windows:
        slices: list[tuple[str, list[Mapping[str, Any]], list[Mapping[str, Any]], list[dict[str, Any]]]] = []
        for strategy, source, collector in (
            ("LIVE_BASELINE", baseline, baseline_oos),
            (candidate[0]["strategy"] if candidate else "CANDIDATE", candidate, candidate_oos),
        ):
            if isinstance(window, TimeWindow):
                train = [row for row in source if window.train_start <= row["_timestamp"] < window.train_end]
                test = [row for row in source if window.test_start <= row["_timestamp"] < window.test_end]
            else:
                train = list(source[window.train_start:window.train_end])
                test = list(source[window.test_start:window.test_end])
            if not test:
                continue
            collector.extend(test)
            slices.append((strategy, train, test, collector))
        metrics_by_strategy = {
            strategy: calculate_metrics(test) for strategy, _, test, _ in slices
        }
        baseline_pf = _finite_pf(metrics_by_strategy.get("LIVE_BASELINE", {}).get("profit_factor"))
        candidate_strategy = candidate[0]["strategy"] if candidate else "CANDIDATE"
        candidate_pf = _finite_pf(metrics_by_strategy.get(candidate_strategy, {}).get("profit_factor"))
        for strategy, train, test, _ in slices:
            metrics = metrics_by_strategy[strategy]
            rows.append({
                "window_id": window.window_id,
                "train_start": train[0]["timestamp"] if train else "",
                "train_end": train[-1]["timestamp"] if train else "",
                "test_start": test[0]["timestamp"],
                "test_end": test[-1]["timestamp"],
                "strategy": strategy,
                "closed_trades": metrics["trades"],
                **metrics,
                "profitable": metrics["net_r"] > 0,
                "candidate_better_than_baseline": candidate_pf > baseline_pf,
                "candidate_profitable": metrics_by_strategy.get(candidate_strategy, {}).get("net_r", 0) > 0,
                "market_regime": _dominant_regime(test),
            })
    return rows, baseline_oos, candidate_oos


def _window_aggregate(
    all_trades: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result = calculate_metrics(all_trades)
    pfs = [_finite_pf(row.get("profit_factor")) for row in rows]
    finite = [value for value in pfs if math.isfinite(value)]
    result.update({
        "profitable_windows": sum(bool(row.get("profitable")) for row in rows),
        "windows": len(rows),
        "average_window_pf": statistics.mean(finite) if finite else None,
        "median_window_pf": statistics.median(finite) if finite else None,
        "worst_window_pf": min(finite) if finite else None,
        "best_window_pf": max(finite) if finite else None,
        "pf_stddev": statistics.pstdev(finite) if len(finite) > 1 else 0.0,
        "windows_pf_above_1": sum(value > 1 for value in pfs),
    })
    return result


def concentration_analysis(
    candidate_rows: Sequence[Mapping[str, Any]],
    baseline_pf: float | None,
) -> tuple[dict[str, Any], list[str]]:
    nets = [float(row.get("net_r", 0)) for row in candidate_rows]
    total = sum(nets)
    positive_total = sum(value for value in nets if value > 0)
    ranked = sorted(nets, reverse=True)
    top1 = ranked[0] / positive_total if ranked and positive_total > 0 else 0.0
    top2 = sum(ranked[:2]) / positive_total if positive_total > 0 else 0.0
    best_index = nets.index(max(nets)) if nets else -1
    worst_index = nets.index(min(nets)) if nets else -1
    warnings = []
    if top1 > 0.5:
        warnings.append("RESULT_CONCENTRATED_IN_SINGLE_WINDOW")
    remaining: list[float] = []
    for index, row in enumerate(candidate_rows):
        if index != best_index:
            remaining.extend([float(row.get("net_r", 0))])
    without_best_pf = profit_factor(remaining)
    if baseline_pf is not None and _finite_pf(without_best_pf) < _finite_pf(baseline_pf):
        warnings.append("FRAGILE_ADVANTAGE")
    return {
        "best_window_contribution_r": max(nets) if nets else 0.0,
        "worst_window_contribution_r": min(nets) if nets else 0.0,
        "top_1_window_profit_share": top1,
        "top_2_windows_profit_share": top2,
        "best_window_index": best_index + 1 if best_index >= 0 else None,
        "net_r_all_windows": total,
        "pf_without_best_window": without_best_pf,
    }, warnings


def regime_analysis(trades: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    groups: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for trade in trades:
        groups[str(trade.get("market_regime") or "unknown")].append(trade)
    result = {regime: calculate_metrics(rows) for regime, rows in sorted(groups.items())}
    known = [metrics for regime, metrics in result.items() if regime != "unknown" and metrics["trades"]]
    warnings = []
    if len(known) >= 2 and sum(metrics["net_r"] > 0 for metrics in known) == 1:
        warnings.append("REGIME_DEPENDENT")
    return result, warnings


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def bootstrap_analysis(
    candidate: Sequence[Mapping[str, Any]], baseline: Sequence[Mapping[str, Any]],
    iterations: int = BOOTSTRAP_ITERATIONS, seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Bootstrap only the final OOS result arrays; window order is untouched."""
    candidate_values = [float(row["result_r"]) for row in candidate]
    baseline_values = [float(row["result_r"]) for row in baseline]
    if not candidate_values:
        return {"iterations": 0, "seed": seed, "status": "INSUFFICIENT_DATA"}
    rng = random.Random(seed)
    nets: list[float] = []
    averages: list[float] = []
    winrates: list[float] = []
    pfs: list[float] = []
    pf_better = net_better = 0
    baseline_observed_pf = _finite_pf(profit_factor(baseline_values))
    for _ in range(max(2000, iterations)):
        c_sample = [rng.choice(candidate_values) for _ in candidate_values]
        b_sample = [rng.choice(baseline_values) for _ in baseline_values] if baseline_values else []
        net = sum(c_sample)
        pf = profit_factor(c_sample)
        finite_pf = _finite_pf(pf)
        nets.append(net)
        averages.append(net / len(c_sample))
        winrates.append(sum(value > 0 for value in c_sample) / len(c_sample) * 100)
        if math.isfinite(finite_pf):
            pfs.append(finite_pf)
        pf_better += finite_pf > baseline_observed_pf
        net_better += net > sum(b_sample)
    count = max(2000, iterations)
    confidence_interval = lambda values: [  # noqa: E731
        _percentile(values, 0.025), _percentile(values, 0.975)
    ]
    return {
        "iterations": count,
        "seed": seed,
        "net_r_95_ci": confidence_interval(nets),
        "average_r_95_ci": confidence_interval(averages),
        "winrate_95_ci": confidence_interval(winrates),
        "profit_factor_95_ci": confidence_interval(pfs) if pfs else None,
        "probability_net_r_above_zero": sum(value > 0 for value in nets) / count,
        "probability_pf_above_baseline_pf": pf_better / count,
        "probability_candidate_net_r_above_baseline_net_r": net_better / count,
    }


def classify_status(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any],
    comparison: Mapping[str, Any], stability: Mapping[str, Any],
    regimes: Mapping[str, Any],
) -> str:
    windows = int(candidate.get("windows", 0))
    trades = int(candidate.get("trades", 0))
    better_share = float(comparison.get("better_windows_share", 0))
    profitable_share = (
        float(candidate.get("profitable_windows", 0)) / windows if windows else 0.0
    )
    candidate_pf = _finite_pf(candidate.get("profit_factor"))
    baseline_pf = _finite_pf(baseline.get("profit_factor"))
    candidate_net = float(candidate.get("net_r", 0))
    baseline_net = float(baseline.get("net_r", 0))
    candidate_dd = float(candidate.get("max_drawdown_r", 0))
    baseline_dd = float(baseline.get("max_drawdown_r", 0))
    dd_ratio = candidate_dd / baseline_dd if baseline_dd > 0 else (math.inf if candidate_dd > 0 else 1.0)
    known_profitable = sum(
        metrics.get("net_r", 0) > 0 for regime, metrics in regimes.items()
        if regime != "unknown" and metrics.get("trades", 0)
    )
    known_regimes = sum(
        bool(metrics.get("trades", 0)) for regime, metrics in regimes.items()
        if regime != "unknown"
    )
    if (
        candidate_pf >= 1.20 and trades >= 150 and windows >= 6
        and profitable_share >= 0.65 and candidate_net > 0
        and candidate_dd < baseline_dd and known_regimes >= 2 and known_profitable >= 2
    ):
        return "READY_FOR_LIMITED_LIVE_REVIEW"
    if (
        candidate_pf >= 1.0 and trades >= 75 and windows >= 4
        and better_share >= 0.60 and candidate_net > 0 and dd_ratio <= 1.10
        and float(stability.get("top_1_window_profit_share", 0)) <= 0.5
    ):
        return "READY_FOR_SHADOW"
    rejected = (
        candidate_pf < baseline_pf or candidate_net < baseline_net
        or better_share < 0.40 or dd_ratio > 1.20
        or windows < 3 or trades < 50
    )
    if rejected:
        return "REJECTED"
    if candidate_pf < 1 and better_share >= 0.50 and dd_ratio <= 1.20:
        return "PROMISING_BUT_UNPROVEN"
    return "PROMISING_BUT_UNPROVEN"


def confidence_label(bootstrap: Mapping[str, Any]) -> str:
    probability = float(bootstrap.get("probability_net_r_above_zero", 0))
    return "HIGH" if probability >= 0.90 else "MEDIUM" if probability >= 0.70 else "LOW"


def _empty_report(
    status: str, candidate_id: str, candidate_config: Mapping[str, Any] | None,
    audit: Mapping[str, Any], configuration: Mapping[str, Any], reason: str | None = None,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reason": reason or status,
        "data_source": audit.get("data_source"),
        "data_audit": dict(audit),
        "configuration": {
            **configuration, "candidate_id": candidate_id,
            "candidate_config": dict(candidate_config) if candidate_config else None,
            "live_configuration_changed": False,
        },
        "baseline": {}, "candidate": {"name": _display_strategy(candidate_id)},
        "comparison": {}, "stability": {}, "bootstrap": {},
        "regimes": {}, "warnings": [status],
        "recommendation": "Do not deploy candidate. Collect valid closed shadow trades and rerun validation.",
    }


def run_validation(
    *, candidate: str = "momentum_relaxed",
    train_size: int = DEFAULT_TRAIN_SIZE, test_size: int = DEFAULT_TEST_SIZE,
    step_size: int = DEFAULT_STEP_SIZE,
    min_test_trades: int = DEFAULT_MIN_TEST_TRADES,
    data_path: str | Path | None = None, config_path: str | Path = CONFIG_FILE,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidate_id, candidate_config = load_candidate_config(candidate, config_path)
    requested = {
        "requested_train_size": train_size, "requested_test_size": test_size,
        "requested_step_size": step_size, "min_test_trades": min_test_trades,
        "minimum_train_size": MIN_TRAIN_SIZE, "minimum_windows": MIN_WINDOWS,
        "bootstrap_iterations": BOOTSTRAP_ITERATIONS, "bootstrap_seed": BOOTSTRAP_SEED,
    }
    source = Path(data_path) if data_path else find_data_source()
    trades, audit = load_trades(source)
    if candidate_config is None:
        return _empty_report(
            "CANDIDATE_CONFIG_NOT_FOUND", candidate_id, None, audit, requested,
        ), []
    baseline = [row for row in trades if row["strategy"] == "LIVE_BASELINE"]
    candidate_rows = [row for row in trades if row["strategy"] == candidate_id]
    audit = {
        **audit,
        "target_strategy_counts": {
            "LIVE_BASELINE": len(baseline), candidate_id: len(candidate_rows),
        },
    }
    audit["filter_stages"] = {
        **audit.get("filter_stages", {}),
        "target_strategies_valid": len(baseline) + len(candidate_rows),
    }
    if not baseline or not candidate_rows:
        if not trades and audit.get("skip_reasons", {}).get("invalid_timestamp"):
            reason = "No valid timestamps remain after filtering."
        elif not baseline:
            reason = f"LIVE_BASELINE has {len(baseline)} valid trades; at least {MIN_TRAIN_SIZE + MIN_WINDOWS * min_test_trades} are required."
        else:
            reason = f"{candidate_id} has {len(candidate_rows)} valid trades; at least {MIN_TRAIN_SIZE + MIN_WINDOWS * min_test_trades} are required."
        return _empty_report(
            "INSUFFICIENT_DATA", candidate_id, candidate_config, audit, requested, reason,
        ), []
    sample_size = min(len(baseline), len(candidate_rows))
    windows, resolved = build_time_windows(
        baseline, candidate_rows, train_size, test_size, step_size, min_test_trades,
    )
    configuration = {
        **requested, **resolved, "sample_size_per_strategy": sample_size,
        "valid_baseline_trades": len(baseline),
        "valid_candidate_trades": len(candidate_rows),
        "window_basis": "shared_utc_time_periods",
    }
    audit["filter_stages"]["walk_forward_eligible"] = len(baseline) + len(candidate_rows)
    if len(windows) < MIN_WINDOWS:
        minimum = resolved["minimum_required_per_strategy"]
        reason = (
            f"Only {len(windows)} shared chronological windows are possible after filtering; "
            f"{MIN_WINDOWS} are required. Valid trades: LIVE_BASELINE={len(baseline)}, "
            f"{candidate_id}={len(candidate_rows)}; minimum per strategy={minimum}."
        )
        return _empty_report(
            "INSUFFICIENT_WALK_FORWARD_WINDOWS", candidate_id,
            candidate_config, audit, configuration, reason,
        ), []
    window_rows, baseline_oos, candidate_oos = evaluate_windows(
        baseline, candidate_rows, windows,
    )
    baseline_windows = [row for row in window_rows if row["strategy"] == "LIVE_BASELINE"]
    candidate_windows = [row for row in window_rows if row["strategy"] == candidate_id]
    baseline_metrics = _window_aggregate(baseline_oos, baseline_windows)
    candidate_metrics = _window_aggregate(candidate_oos, candidate_windows)
    pairs = list(zip(baseline_windows, candidate_windows))
    better = sum(
        _finite_pf(candidate_window["profit_factor"]) > _finite_pf(base_window["profit_factor"])
        for base_window, candidate_window in pairs
    )
    worse = sum(
        _finite_pf(candidate_window["profit_factor"]) < _finite_pf(base_window["profit_factor"])
        for base_window, candidate_window in pairs
    )
    comparison = {
        "pf_improvement": _finite_pf(candidate_metrics["profit_factor"]) - _finite_pf(baseline_metrics["profit_factor"]),
        "net_r_improvement": candidate_metrics["net_r"] - baseline_metrics["net_r"],
        "drawdown_improvement": baseline_metrics["max_drawdown_r"] - candidate_metrics["max_drawdown_r"],
        "winrate_improvement": candidate_metrics["winrate"] - baseline_metrics["winrate"],
        "candidate_better_windows": better,
        "candidate_worse_windows": worse,
        "equal_windows": len(pairs) - better - worse,
        "better_windows_share": better / len(pairs) if pairs else 0.0,
    }
    stability, warnings = concentration_analysis(
        candidate_windows, baseline_metrics["profit_factor"],
    )
    regimes, regime_warnings = regime_analysis(candidate_oos)
    warnings.extend(regime_warnings)
    bootstrap = bootstrap_analysis(candidate_oos, baseline_oos)
    if bootstrap.get("probability_net_r_above_zero", 0) < 0.70:
        warnings.append("LOW_STATISTICAL_CONFIDENCE")
    status = classify_status(
        baseline_metrics, candidate_metrics, comparison, stability, regimes,
    )
    if "REGIME_DEPENDENT" in warnings and status in {
        "READY_FOR_SHADOW", "READY_FOR_LIMITED_LIVE_REVIEW",
    }:
        status = "PROMISING_BUT_UNPROVEN"
    recommendation = {
        "REJECTED": "Reject candidate for now. Keep the live strategy unchanged.",
        "PROMISING_BUT_UNPROVEN": "Continue shadow research. Do not deploy candidate.",
        "READY_FOR_SHADOW": "Candidate may continue in shadow mode. Do not apply to live strategy.",
        "READY_FOR_LIMITED_LIVE_REVIEW": "Submit for manual limited-live review only. Do not apply automatically.",
    }[status]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reason": "Validation completed with shared chronological OOS windows.",
        "data_source": str(source),
        "data_audit": audit,
        "configuration": {
            **configuration, "candidate_id": candidate_id,
            "candidate_config": candidate_config,
            "live_configuration_changed": False,
        },
        "baseline": baseline_metrics,
        "candidate": {"name": _display_strategy(candidate_id), **candidate_metrics},
        "comparison": comparison,
        "stability": stability,
        "bootstrap": {**bootstrap, "confidence": confidence_label(bootstrap)},
        "regimes": regimes,
        "warnings": sorted(set(warnings)),
        "recommendation": recommendation,
    }
    return _json_value(report), [_json_value(row) for row in window_rows]


def format_summary(report: Mapping[str, Any]) -> str:
    baseline = report.get("baseline", {})
    candidate = report.get("candidate", {})
    comparison = report.get("comparison", {})
    audit = report.get("data_audit", {})
    windows = int(candidate.get("windows", 0) or 0)
    lines = [
        "WALK-FORWARD VALIDATION",
        f"Candidate: {candidate.get('name', 'N/A')}",
        f"Status: {report.get('status', 'INSUFFICIENT_DATA')}",
        f"Reason: {report.get('reason', 'N/A')}",
        f"Data Source: {report.get('data_source', 'N/A')}",
        f"Rows Read: {audit.get('rows_read', 0)}",
        f"Valid Closed Trades: {audit.get('valid_closed_trades', 0)}",
        f"Closed Rows: {audit.get('closed_rows', 0)}",
        f"Rows By Strategy: {json.dumps(audit.get('rows_by_strategy', {}), ensure_ascii=False)}",
        f"Valid By Strategy: {json.dumps(audit.get('valid_by_strategy', {}), ensure_ascii=False)}",
        f"Rows Skipped: {audit.get('rows_skipped', 0)}",
        f"Skip Reasons: {json.dumps(audit.get('skip_reasons', {}), ensure_ascii=False)}",
        f"Windows: {windows}",
        f"Out-of-Sample Trades: {candidate.get('trades', 0)}",
        "Baseline Out-of-Sample:",
        f"  Trades: {baseline.get('trades', 0)}",
        f"  PF: {_format_pf(baseline.get('profit_factor'))}",
        f"  Winrate: {float(baseline.get('winrate', 0)):.2f}%",
        f"  Net R: {float(baseline.get('net_r', 0)):.2f}R",
        f"  Max Drawdown: {float(baseline.get('max_drawdown_r', 0)):.2f}R",
        f"  Profitable Windows: {baseline.get('profitable_windows', 0)} / {baseline.get('windows', 0)}",
        f"{candidate.get('name', 'Candidate')} Out-of-Sample:",
        f"  Trades: {candidate.get('trades', 0)}",
        f"  PF: {_format_pf(candidate.get('profit_factor'))}",
        f"  Winrate: {float(candidate.get('winrate', 0)):.2f}%",
        f"  Net R: {float(candidate.get('net_r', 0)):.2f}R",
        f"  Max Drawdown: {float(candidate.get('max_drawdown_r', 0)):.2f}R",
        f"  Better Windows: {comparison.get('candidate_better_windows', 0)} / {windows}",
        f"  Profitable Windows: {candidate.get('profitable_windows', 0)} / {windows}",
        f"Confidence: {report.get('bootstrap', {}).get('confidence', 'LOW')}",
        f"Warnings: {', '.join(report.get('warnings', [])) or 'NONE'}",
        f"Recommendation: {report.get('recommendation', 'Do not deploy candidate.')}",
        "Live Configuration Changed: NO",
    ]
    return "\n".join(lines) + "\n"


def _format_pf(value: Any) -> str:
    if value in ("INF", math.inf):
        return "INF"
    number = _number(value)
    return f"{number:.2f}" if number is not None else "N/A"


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_outputs(
    report: Mapping[str, Any], windows: Sequence[Mapping[str, Any]],
    report_path: str | Path = REPORT_FILE, summary_path: str | Path = SUMMARY_FILE,
    windows_path: str | Path = WINDOWS_FILE,
) -> None:
    _atomic_text(
        Path(report_path), json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )
    _atomic_text(Path(summary_path), format_summary(report))
    temporary_lines: list[str] = []
    from io import StringIO
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=WINDOW_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(windows)
    temporary_lines.append(buffer.getvalue())
    _atomic_text(Path(windows_path), "".join(temporary_lines))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", default="momentum_relaxed")
    parser.add_argument("--train-size", type=int, default=DEFAULT_TRAIN_SIZE)
    parser.add_argument("--test-size", type=int, default=DEFAULT_TEST_SIZE)
    parser.add_argument("--step-size", type=int, default=DEFAULT_STEP_SIZE)
    parser.add_argument("--min-test-trades", type=int, default=DEFAULT_MIN_TEST_TRADES)
    parser.add_argument("--data-file", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report, windows = run_validation(
        candidate=args.candidate, train_size=args.train_size,
        test_size=args.test_size, step_size=args.step_size,
        min_test_trades=args.min_test_trades, data_path=args.data_file,
    )
    save_outputs(report, windows)
    print(format_summary(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
