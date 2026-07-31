#!/usr/bin/env python3
"""Read-only audit of Candidate Laboratory walk-forward input and windows."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import walk_forward_validation as wfv


ALIASES = {
    "strategy": ["candidate_id", "strategy_name", "strategy", "candidate"],
    "status": ["status", "result", "trade_status"],
    "timestamp": ["closed_at", "close_time", "exit_time", "timestamp", "time", "opened_at"],
    "close_time": ["closed_at", "close_time", "exit_time"],
    "pnl_r": ["pnl_r", "result_r", "net_r", "r_result", "rr_result"],
    "shadow_trade_id": ["shadow_trade_id", "trade_id", "id"],
}
TARGETS = ("LIVE_BASELINE", "MOMENTUM_RELAXED")


def _first(row: Mapping[str, Any], names: Sequence[str]) -> Any:
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    return next((lowered[name] for name in names if lowered.get(name) not in (None, "")), None)


def _timestamp_kind(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "missing"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return "invalid"
    return "aware" if parsed.tzinfo is not None else "naive"


def _distribution(trades: Sequence[Mapping[str, Any]], unit: str) -> dict[str, int]:
    pattern = "%Y-%m-%d" if unit == "day" else "%Y-%m-%dT%H:00Z"
    return dict(sorted(Counter(row["_timestamp"].strftime(pattern) for row in trades).items()))


def _strategy_stats(
    strategy: str, raw_rows: Sequence[Mapping[str, Any]], trades: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    raw = [row for row in raw_rows if wfv._canonical_strategy(_first(row, ALIASES["strategy"])) == strategy]
    valid = [row for row in trades if row["strategy"] == strategy]
    kinds = Counter(_timestamp_kind(_first(row, ALIASES["timestamp"])) for row in raw)
    timestamps = [row["_timestamp"] for row in valid]
    return {
        "count": len(valid),
        "raw_count": len(raw),
        "first_close_timestamp": min(timestamps).isoformat() if timestamps else None,
        "last_close_timestamp": max(timestamps).isoformat() if timestamps else None,
        "unique_timestamps": len(set(timestamps)),
        "missing_timestamps": kinds["missing"],
        "invalid_timestamps": kinds["invalid"],
        "naive_timestamps": kinds["naive"],
        "utc_aware_timestamps": kinds["aware"],
        "trades_by_day": _distribution(valid, "day"),
        "trades_by_hour": _distribution(valid, "hour"),
    }


def _overlap(baseline: Sequence[Mapping[str, Any]], candidate: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not baseline or not candidate:
        return {"exists": False, "start": None, "end": None, "baseline_trades": 0,
                "candidate_trades": 0, "days": 0, "hours": 0}
    start = max(baseline[0]["_timestamp"], candidate[0]["_timestamp"])
    end = min(baseline[-1]["_timestamp"], candidate[-1]["_timestamp"])
    if start > end:
        return {"exists": False, "start": None, "end": None, "baseline_trades": 0,
                "candidate_trades": 0, "days": 0, "hours": 0}
    duration = end - start
    return {
        "exists": True, "start": start.isoformat(), "end": end.isoformat(),
        "baseline_trades": sum(start <= row["_timestamp"] <= end for row in baseline),
        "candidate_trades": sum(start <= row["_timestamp"] <= end for row in candidate),
        "days": duration.days + 1,
        "hours": int(duration.total_seconds() // 3600) + 1,
    }


def _window_row(window: wfv.TimeWindow, baseline: Sequence[Mapping[str, Any]], candidate: Sequence[Mapping[str, Any]],
                train_min: int, test_min: int) -> dict[str, Any]:
    def count(rows: Sequence[Mapping[str, Any]], start: datetime, end: datetime) -> int:
        return sum(start <= row["_timestamp"] < end for row in rows)
    bt = count(baseline, window.train_start, window.train_end)
    bo = count(baseline, window.test_start, window.test_end)
    ct = count(candidate, window.train_start, window.train_end)
    co = count(candidate, window.test_start, window.test_end)
    reasons = []
    if bt < train_min: reasons.append(f"baseline train {bt} < {train_min}")
    if ct < train_min: reasons.append(f"candidate train {ct} < {train_min}")
    if bo < test_min: reasons.append(f"baseline test {bo} < {test_min}")
    if co < test_min: reasons.append(f"candidate test {co} < {test_min}")
    if window.train_end > window.test_start: reasons.append("train/test overlap (leakage)")
    return {
        "window": window.window_id,
        "train_start": window.train_start.isoformat(), "train_end": window.train_end.isoformat(),
        "test_start": window.test_start.isoformat(), "test_end": window.test_end.isoformat(),
        "baseline_train_count": bt, "baseline_test_count": bo,
        "candidate_train_count": ct, "candidate_test_count": co,
        "accepted": not reasons, "rejection_reasons": reasons,
    }


def _strict_window_count(baseline: Sequence[Mapping[str, Any]], candidate: Sequence[Mapping[str, Any]],
                         train_size: int, test_size: int) -> int:
    """Count shared windows without adaptive parameter reduction."""
    timestamps = sorted({row["_timestamp"] for row in [*baseline, *candidate]})
    if not timestamps:
        return 0
    boundary_index = next((index for index, stamp in enumerate(timestamps)
                           if sum(row["_timestamp"] < stamp for row in baseline) >= train_size
                           and sum(row["_timestamp"] < stamp for row in candidate) >= train_size), None)
    if boundary_index is None:
        return 0
    count = 0
    test_start = timestamps[boundary_index]
    while count < wfv.MIN_WINDOWS:
        endpoints = timestamps[boundary_index + 1:] + [timestamps[-1] + timedelta(microseconds=1)]
        test_end = next((stamp for stamp in endpoints
                         if sum(test_start <= row["_timestamp"] < stamp for row in baseline) >= test_size
                         and sum(test_start <= row["_timestamp"] < stamp for row in candidate) >= test_size), None)
        if test_end is None:
            break
        count += 1
        if count >= wfv.MIN_WINDOWS or test_end not in timestamps:
            break
        boundary_index = timestamps.index(test_end)
        test_start = test_end
    return count


def diagnose(path: str | Path, *, train_size: int = wfv.DEFAULT_TRAIN_SIZE,
             test_size: int = wfv.DEFAULT_TEST_SIZE, step_size: int = wfv.DEFAULT_STEP_SIZE,
             min_test_trades: int = wfv.DEFAULT_MIN_TEST_TRADES) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    source_info = {"absolute_path": str(source), "exists": source.exists(), "size_bytes": None, "line_count": 0}
    if not source.exists():
        return {"source": source_info, "parsing": {"error": "file_not_found"},
                "final_verdict": {"reason": "Input file does not exist.", "can_create_three_windows": False}}
    source_info["size_bytes"] = source.stat().st_size
    try:
        with source.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            columns = list(reader.fieldnames or [])
            raw_rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        return {"source": source_info, "parsing": {"error": str(exc)},
                "final_verdict": {"reason": "CSV could not be read.", "can_create_three_windows": False}}
    source_info["line_count"] = len(raw_rows) + (1 if columns else 0)
    trades, audit = wfv.prepare_trades(raw_rows)

    identifiers = [str(_first(row, ALIASES["shadow_trade_id"]) or "").strip() for row in raw_rows]
    id_counts = Counter(value for value in identifiers if value)
    duplicate_ids = {key: count for key, count in id_counts.items() if count > 1}
    parsing = {
        "rows_read": audit["rows_read"], "closed_rows": audit["closed_rows"],
        "valid_rows": audit["valid_closed_trades"], "rejected_rows": audit["rows_skipped"],
        "rejection_reasons": audit["skip_reasons"], "columns": columns, "aliases": ALIASES,
    }
    deduplication = {
        "unique_shadow_trade_ids": len(id_counts),
        "missing_shadow_trade_ids": sum(not value for value in identifiers),
        "duplicate_shadow_trade_ids": sum(count - 1 for count in duplicate_ids.values()),
        "duplicate_examples": [{"shadow_trade_id": key, "occurrences": count}
                               for key, count in list(sorted(duplicate_ids.items()))[:10]],
    }
    baseline = [row for row in trades if row["strategy"] == TARGETS[0]]
    candidate = [row for row in trades if row["strategy"] == TARGETS[1]]
    overlap = _overlap(baseline, candidate)
    windows, resolved = wfv.build_time_windows(
        baseline, candidate, train_size, test_size, step_size, min_test_trades,
    )
    window_audit = [_window_row(window, baseline, candidate, resolved["train_size"],
                                resolved["test_size"]) for window in windows]
    required = wfv.MIN_TRAIN_SIZE + wfv.MIN_WINDOWS * min_test_trades
    if len(window_audit) < wfv.MIN_WINDOWS:
        reason = (
            "No common time overlap." if not overlap["exists"] else
            f"Only {len(window_audit)} shared non-overlapping windows can satisfy chronological counts; "
            f"{wfv.MIN_WINDOWS} are required."
        )
        window_audit.append({
            "window": len(window_audit) + 1, "accepted": False,
            "rejection_reasons": [reason],
            "baseline_train_count": len(baseline), "baseline_test_count": 0,
            "candidate_train_count": len(candidate), "candidate_test_count": 0,
            "train_start": None, "train_end": None, "test_start": None, "test_end": None,
        })
    sample = min(len(baseline), len(candidate))
    max_train = max((size for size in range(wfv.MIN_TRAIN_SIZE, sample + 1)
                     if _strict_window_count(baseline, candidate, size, min_test_trades) >= wfv.MIN_WINDOWS),
                    default=0)
    max_test = max((size for size in range(min_test_trades, sample + 1)
                    if _strict_window_count(baseline, candidate, wfv.MIN_TRAIN_SIZE, size) >= wfv.MIN_WINDOWS),
                   default=0)
    can_create = len(windows) >= wfv.MIN_WINDOWS and all(row["accepted"] for row in window_audit[:wfv.MIN_WINDOWS])
    verdict_reason = (
        "At least three leakage-free shared OOS windows are feasible."
        if can_create else window_audit[-1]["rejection_reasons"][0]
    )
    return {
        "source": source_info, "parsing": parsing, "deduplication": deduplication,
        "strategies": {strategy: _strategy_stats(strategy, raw_rows, trades) for strategy in TARGETS},
        "overlap": overlap,
        "window_config": {
            "requested": {"train": train_size, "test": test_size, "step": step_size},
            "effective": {"train": resolved["train_size"], "test": resolved["test_size"],
                          "step": resolved["step_size"]},
            "minimum_train_per_strategy": wfv.MIN_TRAIN_SIZE,
            "minimum_test_per_window_per_strategy": min_test_trades,
            "minimum_windows": wfv.MIN_WINDOWS, "minimum_required_per_strategy": required,
            "potential_windows": len(windows),
        },
        "window_audit": window_audit,
        "final_verdict": {
            "reason": verdict_reason, "can_create_three_windows": can_create,
            "maximum_honest_train_size_for_three_minimum_tests": max_train,
            "maximum_honest_test_size_with_minimum_train": max_test,
            "leakage_free_parameters": {
                "train": wfv.MIN_TRAIN_SIZE, "test": min_test_trades,
                "step": min_test_trades, "windows": wfv.MIN_WINDOWS,
            } if can_create else None,
            "minimums_were_lowered": False,
        },
    }


def format_report(report: Mapping[str, Any]) -> str:
    sections = []
    for name in ("source", "parsing", "deduplication", "strategies", "overlap",
                 "window_config", "window_audit", "final_verdict"):
        sections.extend([name.upper().replace("_", " "), json.dumps(report.get(name, {}),
                         ensure_ascii=False, indent=2), ""])
    return "\n".join(sections)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--train-size", type=int, default=wfv.DEFAULT_TRAIN_SIZE)
    parser.add_argument("--test-size", type=int, default=wfv.DEFAULT_TEST_SIZE)
    parser.add_argument("--step-size", type=int, default=wfv.DEFAULT_STEP_SIZE)
    parser.add_argument("--min-test-trades", type=int, default=wfv.DEFAULT_MIN_TEST_TRADES)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = diagnose(args.input, train_size=args.train_size, test_size=args.test_size,
                      step_size=args.step_size, min_test_trades=args.min_test_trades)
    print(format_report(report), end="")
    if args.json_output:
        output = args.json_output.expanduser().resolve()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
