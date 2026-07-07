"""Dry-Run Outcome Analyzer v1 for AITradingAgent.

This module checks whether read-only dry-run candidates would have produced
favorable price movement or simple TP/SL outcomes. It does not change
DecisionEngine, config.py, thresholds, weights, the live agent, Telegram, or
trade execution.
"""

from __future__ import annotations

import csv
import json
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable


BASE_DIR = Path(__file__).resolve().parent
OHLCV_CACHE_DIR = BASE_DIR / "ohlcv_cache"

REPORT_PATH = BASE_DIR / "dry_run_outcome_report.json"
SUMMARY_PATH = BASE_DIR / "dry_run_outcome_summary.txt"
BY_TYPE_CSV_PATH = BASE_DIR / "dry_run_outcome_by_type.csv"
CANDIDATES_CSV_PATH = BASE_DIR / "dry_run_outcome_candidates.csv"
SIMULATION_CSV_PATH = BASE_DIR / "dry_run_outcome_trade_simulation.csv"

HORIZONS: dict[str, int] = {
    "1h": 1,
    "2h": 2,
    "4h": 4,
    "8h": 8,
    "12h": 12,
    "24h": 24,
}
SIMULATION_HORIZONS: dict[str, int] = {"8h": 8, "24h": 24}

DRY_RUN_SOURCES: dict[str, str] = {
    "relaxed_edge": "relaxed_edge_dry_run.csv",
    "long_rebound": "long_rebound_opportunity_dry_run.csv",
    "ada_opportunity": "ada_opportunity_dry_run.csv",
    "doge_link_opportunity": "doge_link_opportunity_dry_run.csv",
}

TYPE_LABELS: dict[str, str] = {
    "relaxed_edge": "Relaxed Edge",
    "long_rebound": "Long Rebound",
    "ada_opportunity": "ADA Opportunity",
    "doge_link_opportunity": "DOGE/LINK Opportunity",
}

INPUT_FILES = [
    *DRY_RUN_SOURCES.values(),
    "signals_v3.csv",
    "decision_debug.csv",
    "trades.csv",
]

Candidate = dict[str, Any]
Candle = dict[str, Any]


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value: Any) -> datetime | None:
    """Parse common project timestamps as timezone-aware UTC datetimes."""
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
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a value to float without raising."""
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def safe_round(value: float | None, digits: int = 4) -> float | None:
    """Round numeric values while preserving None."""
    if value is None:
        return None
    return round(float(value), digits)


def mean(values: Iterable[float]) -> float | None:
    """Return a rounded mean or None for an empty sequence."""
    items = [float(value) for value in values]
    if not items:
        return None
    return round(sum(items) / len(items), 4)


def median_value(values: Iterable[float]) -> float | None:
    """Return a rounded median or None for an empty sequence."""
    items = [float(value) for value in values]
    if not items:
        return None
    return round(float(median(items)), 4)


def percent(part: int | float, total: int | float) -> float:
    """Return percent rounded to two decimals."""
    if not total:
        return 0.0
    return round((float(part) / float(total)) * 100.0, 2)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read CSV rows and tolerate missing or empty files."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with path.open("r", newline="", encoding="utf-8") as file:
            return [
                dict(row)
                for row in csv.DictReader(file)
                if row and any(str(value or "").strip() for value in row.values())
            ]
    except OSError:
        return []


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON in a stable UTF-8 format."""
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    """Write rows to CSV with a guaranteed header."""
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def symbol_key(symbol: str) -> str:
    """Convert BTC/USDT to the OHLCV cache file stem BTC_USDT."""
    return str(symbol or "").upper().replace("/", "_")


def compact_reason(value: Any, limit: int = 600) -> str:
    """Keep long CSV reasons readable."""
    text = " | ".join(
        part.strip()
        for part in str(value or "").splitlines()
        if part.strip()
    )
    return text[:limit]


class OHLCVCache:
    """Local 1h OHLCV cache reader with ATR and outcome helpers."""

    def __init__(self, cache_dir: Path = OHLCV_CACHE_DIR) -> None:
        self.cache_dir = cache_dir
        self._rows: dict[str, list[Candle]] = {}
        self._timestamps: dict[str, list[datetime]] = {}

    def load(self, symbol: str) -> list[Candle]:
        """Load cached candles for a symbol."""
        key = symbol_key(symbol)
        if key in self._rows:
            return self._rows[key]

        path = self.cache_dir / f"{key}_1h.csv"
        rows: list[Candle] = []
        for row in read_csv_rows(path):
            timestamp = parse_dt(row.get("timestamp"))
            if timestamp is None:
                continue
            rows.append({
                "timestamp": timestamp,
                "open": safe_float(row.get("open")),
                "high": safe_float(row.get("high")),
                "low": safe_float(row.get("low")),
                "close": safe_float(row.get("close")),
                "volume": safe_float(row.get("volume")),
            })

        rows.sort(key=lambda item: item["timestamp"])
        self._rows[key] = rows
        self._timestamps[key] = [row["timestamp"] for row in rows]
        return rows

    def find_entry_index(self, symbol: str, timestamp: datetime) -> int | None:
        """Find the candle at or immediately before the signal timestamp."""
        rows = self.load(symbol)
        if not rows:
            return None
        key = symbol_key(symbol)
        index = bisect_right(self._timestamps[key], timestamp) - 1
        if index < 0 or index >= len(rows):
            return None
        return index

    def find_target_index(
        self,
        symbol: str,
        timestamp: datetime,
        horizon_hours: int,
    ) -> int | None:
        """Find the candle at or immediately before the target horizon."""
        rows = self.load(symbol)
        if not rows:
            return None
        target_time = timestamp + timedelta(hours=horizon_hours)
        key = symbol_key(symbol)
        index = bisect_right(self._timestamps[key], target_time) - 1
        if index < 0 or index >= len(rows):
            return None
        return index

    def calculate_atr(
        self,
        symbol: str,
        entry_index: int,
        lookback: int = 14,
    ) -> float | None:
        """Calculate ATR with an average range fallback."""
        rows = self.load(symbol)
        if not rows or entry_index < 0:
            return None

        start = max(1, entry_index - lookback + 1)
        true_ranges: list[float] = []
        for index in range(start, entry_index + 1):
            current = rows[index]
            previous_close = rows[index - 1]["close"]
            true_range = max(
                current["high"] - current["low"],
                abs(current["high"] - previous_close),
                abs(current["low"] - previous_close),
            )
            if true_range > 0:
                true_ranges.append(true_range)

        atr = mean(true_ranges)
        if atr and atr > 0:
            return atr

        range_start = max(0, entry_index - lookback + 1)
        ranges = [
            row["high"] - row["low"]
            for row in rows[range_start:entry_index + 1]
            if row["high"] > row["low"]
        ]
        fallback = mean(ranges)
        return fallback if fallback and fallback > 0 else None

    def outcome_return(
        self,
        candidate: Candidate,
        horizon_hours: int,
    ) -> dict[str, Any]:
        """Calculate direction-aware return for one horizon."""
        timestamp = parse_dt(candidate.get("timestamp"))
        symbol = str(candidate.get("symbol", ""))
        direction = str(candidate.get("direction", "")).upper()
        if timestamp is None or direction not in {"LONG", "SHORT"}:
            return {"available": False, "reason": "нет timestamp или direction"}

        entry_index = self.find_entry_index(symbol, timestamp)
        target_index = self.find_target_index(symbol, timestamp, horizon_hours)
        rows = self.load(symbol)
        if entry_index is None or target_index is None:
            return {"available": False, "reason": "нет OHLCV для горизонта"}
        if target_index <= entry_index:
            return {"available": False, "reason": "недостаточно будущих свечей"}

        entry = rows[entry_index]["close"]
        future = rows[target_index]["close"]
        if entry <= 0:
            return {"available": False, "reason": "некорректная entry price"}

        raw_return = ((future - entry) / entry) * 100.0
        direction_return = raw_return if direction == "LONG" else -raw_return
        return {
            "available": True,
            "entry_price": round(entry, 8),
            "future_price": round(future, 8),
            "raw_return": round(raw_return, 4),
            "direction_return": round(direction_return, 4),
            "favorable": direction_return > 0,
        }

    def simulate_trade(
        self,
        candidate: Candidate,
        horizon_hours: int,
    ) -> dict[str, Any]:
        """Run a simple Entry/1 ATR SL/2 ATR TP simulation."""
        timestamp = parse_dt(candidate.get("timestamp"))
        symbol = str(candidate.get("symbol", ""))
        direction = str(candidate.get("direction", "")).upper()
        if timestamp is None or direction not in {"LONG", "SHORT"}:
            return {
                "available": False,
                "outcome": "NO_DATA",
                "reason": "нет timestamp или direction",
            }

        entry_index = self.find_entry_index(symbol, timestamp)
        target_index = self.find_target_index(symbol, timestamp, horizon_hours)
        rows = self.load(symbol)
        if entry_index is None or target_index is None or target_index <= entry_index:
            return {
                "available": False,
                "outcome": "NO_DATA",
                "reason": "нет будущих OHLCV",
            }

        entry = rows[entry_index]["close"]
        atr = self.calculate_atr(symbol, entry_index)
        if not atr or atr <= 0 or entry <= 0:
            return {
                "available": False,
                "outcome": "NO_DATA",
                "reason": "ATR недоступен",
            }

        if direction == "LONG":
            stop_loss = entry - atr
            take_profit = entry + (2.0 * atr)
        else:
            stop_loss = entry + atr
            take_profit = entry - (2.0 * atr)

        for candle in rows[entry_index + 1:target_index + 1]:
            if direction == "LONG":
                touched_sl = candle["low"] <= stop_loss
                touched_tp = candle["high"] >= take_profit
            else:
                touched_sl = candle["high"] >= stop_loss
                touched_tp = candle["low"] <= take_profit

            if touched_sl and touched_tp:
                return self._simulation_result(
                    "SL_FIRST",
                    -1.0,
                    entry,
                    stop_loss,
                    take_profit,
                    atr,
                    "TP и SL в одной свече; выбран консервативный SL",
                )
            if touched_sl:
                return self._simulation_result(
                    "SL_FIRST",
                    -1.0,
                    entry,
                    stop_loss,
                    take_profit,
                    atr,
                    "SL достигнут первым",
                )
            if touched_tp:
                return self._simulation_result(
                    "TP_FIRST",
                    2.0,
                    entry,
                    stop_loss,
                    take_profit,
                    atr,
                    "TP достигнут первым",
                )

        last_close = rows[target_index]["close"]
        if direction == "LONG":
            r_value = (last_close - entry) / atr
        else:
            r_value = (entry - last_close) / atr
        return self._simulation_result(
            "NEITHER",
            round(r_value, 4),
            entry,
            stop_loss,
            take_profit,
            atr,
            "TP/SL не достигнуты на горизонте",
        )

    @staticmethod
    def _simulation_result(
        outcome: str,
        r_value: float,
        entry: float,
        stop_loss: float,
        take_profit: float,
        atr: float,
        reason: str,
    ) -> dict[str, Any]:
        """Build a normalized simulation result."""
        return {
            "available": True,
            "outcome": outcome,
            "r_value": round(r_value, 4),
            "entry_price": round(entry, 8),
            "stop_loss": round(stop_loss, 8),
            "take_profit": round(take_profit, 8),
            "atr": round(atr, 8),
            "reason": reason,
        }


class DryRunOutcomeAnalyzer:
    """Analyze dry-run candidates against local OHLCV outcomes."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.ohlcv = OHLCVCache(base_dir / "ohlcv_cache")
        self.warnings: list[str] = []
        self.source_status = self._source_status()

    def build_report(self) -> dict[str, Any]:
        """Build, save, and return the dry-run outcome report."""
        candidates = self._load_candidates()
        enriched_candidates = self._analyze_candidates(candidates)
        simulation_rows = self._simulate_candidates(enriched_candidates)
        by_type = self._aggregate_by_type(enriched_candidates, simulation_rows)
        overall = self._overall_metrics(enriched_candidates, simulation_rows)
        live_comparison = self._live_strategy_comparison(
            enriched_candidates,
            simulation_rows,
        )

        report = {
            "generated_at": utc_now(),
            "status": self._determine_status(overall),
            "source_status": self.source_status,
            "warnings": self.warnings,
            "summary": {
                "total_candidates": len(enriched_candidates),
                "candidate_counts_by_type": dict(Counter(
                    candidate["dry_run_type"]
                    for candidate in enriched_candidates
                )),
                "best_dry_run_type": overall.get("best_dry_run_type"),
                "best_symbol": overall.get("best_symbol"),
                "recommendation": self._recommendation_text(overall),
            },
            "outcome_metrics": overall.get("outcome_metrics", {}),
            "trade_simulation": overall.get("trade_simulation", {}),
            "by_type": by_type,
            "live_strategy_comparison": live_comparison,
            "criteria": {
                "minimum_sample_size": 30,
                "minimum_profit_factor": 1.2,
                "minimum_favorable_rate_4h": 55.0,
                "minimum_avg_return_4h": 0.0,
            },
        }

        write_json(REPORT_PATH, report)
        self._write_candidate_csv(enriched_candidates)
        self._write_simulation_csv(simulation_rows)
        self._write_by_type_csv(by_type)
        SUMMARY_PATH.write_text(self._format_summary(report), encoding="utf-8")
        return report

    def print_report(self) -> None:
        """Print the generated summary to the terminal."""
        report = self.build_report()
        summary = self._format_summary(report)
        print(summary)

    def _source_status(self) -> dict[str, dict[str, Any]]:
        """Collect presence and row-count information for source files."""
        status: dict[str, dict[str, Any]] = {}
        for filename in INPUT_FILES:
            path = self.base_dir / filename
            exists = path.exists()
            row_count = 0
            if exists and path.suffix.lower() == ".csv":
                row_count = len(read_csv_rows(path))
            if not exists:
                self.warnings.append(f"{filename}: файл отсутствует.")
            elif path.suffix.lower() == ".csv" and row_count == 0:
                self.warnings.append(f"{filename}: файл пустой или содержит только заголовок.")
            status[filename] = {
                "exists": exists,
                "size_bytes": path.stat().st_size if exists else 0,
                "row_count": row_count,
                "message": self._source_message(path, row_count),
            }
        cache_files = sorted(OHLCV_CACHE_DIR.glob("*_1h.csv"))
        status["ohlcv_cache"] = {
            "exists": OHLCV_CACHE_DIR.exists(),
            "file_count": len(cache_files),
            "message": (
                f"найдено cache-файлов: {len(cache_files)}"
                if cache_files
                else "OHLCV cache не найден"
            ),
        }
        return status

    @staticmethod
    def _source_message(path: Path, row_count: int) -> str:
        if not path.exists():
            return "файл отсутствует"
        if path.suffix.lower() == ".csv" and row_count == 0:
            return "файл есть, но строк с данными пока нет"
        return "файл найден"

    def _load_candidates(self) -> list[Candidate]:
        """Load and normalize all dry-run candidates."""
        candidates: list[Candidate] = []
        for dry_run_type, filename in DRY_RUN_SOURCES.items():
            path = self.base_dir / filename
            rows = read_csv_rows(path)
            if not path.exists():
                continue
            if not rows:
                continue
            for row in rows:
                normalized = self._normalize_candidate(dry_run_type, row)
                if normalized is not None:
                    candidates.append(normalized)
        return candidates

    def _normalize_candidate(
        self,
        dry_run_type: str,
        row: dict[str, str],
    ) -> Candidate | None:
        """Normalize different dry-run CSV schemas to one candidate model."""
        timestamp = parse_dt(row.get("timestamp"))
        symbol = str(row.get("symbol", "")).strip().upper()
        if timestamp is None or not symbol:
            self.warnings.append(
                f"{TYPE_LABELS[dry_run_type]}: строка без timestamp/symbol пропущена."
            )
            return None

        long_score = safe_float(row.get("long_score"))
        short_score = safe_float(row.get("short_score"))
        raw_direction = str(row.get("direction", "")).strip().upper()
        direction = self._effective_direction(dry_run_type, raw_direction, long_score, short_score)
        edge = safe_float(row.get("edge"), safe_float(row.get("diff")))

        return {
            "candidate_id": f"{dry_run_type}:{timestamp.isoformat()}:{symbol}",
            "dry_run_type": dry_run_type,
            "dry_run_label": TYPE_LABELS[dry_run_type],
            "timestamp": timestamp.isoformat(),
            "symbol": symbol,
            "direction": direction,
            "raw_direction": raw_direction or direction,
            "decision": str(row.get("decision", "")).strip().upper(),
            "status": str(row.get("status", "")).strip().upper(),
            "score": safe_float(row.get("score")),
            "confidence": safe_float(row.get("confidence")),
            "weighted_score": safe_float(row.get("weighted_score")),
            "edge": edge,
            "edge_gap": safe_float(row.get("edge_gap")),
            "long_score": long_score,
            "short_score": short_score,
            "near_setup_category": str(row.get("near_setup_category", "")).strip(),
            "filter_name": str(row.get("filter_name", "")).strip(),
            "reason": compact_reason(row.get("reason")),
        }

    @staticmethod
    def _effective_direction(
        dry_run_type: str,
        raw_direction: str,
        long_score: float,
        short_score: float,
    ) -> str:
        """Choose a tradable direction for direction-aware outcome checks."""
        if raw_direction in {"LONG", "SHORT"}:
            return raw_direction
        if dry_run_type == "long_rebound":
            return "LONG"
        if long_score > short_score:
            return "LONG"
        if short_score > long_score:
            return "SHORT"
        return "NEUTRAL"

    def _analyze_candidates(self, candidates: list[Candidate]) -> list[Candidate]:
        """Attach horizon outcomes to candidates."""
        enriched: list[Candidate] = []
        for candidate in candidates:
            outcomes: dict[str, dict[str, Any]] = {}
            for label, hours in HORIZONS.items():
                outcomes[label] = self.ohlcv.outcome_return(candidate, hours)
            next_candidate = dict(candidate)
            next_candidate["outcomes"] = outcomes
            enriched.append(next_candidate)
        return enriched

    def _simulate_candidates(self, candidates: list[Candidate]) -> list[dict[str, Any]]:
        """Run TP/SL simulations for all candidates."""
        rows: list[dict[str, Any]] = []
        for candidate in candidates:
            for label, hours in SIMULATION_HORIZONS.items():
                result = self.ohlcv.simulate_trade(candidate, hours)
                rows.append({
                    "candidate_id": candidate["candidate_id"],
                    "dry_run_type": candidate["dry_run_type"],
                    "timestamp": candidate["timestamp"],
                    "symbol": candidate["symbol"],
                    "direction": candidate["direction"],
                    "horizon": label,
                    "available": result.get("available", False),
                    "outcome": result.get("outcome", "NO_DATA"),
                    "r_value": result.get("r_value"),
                    "entry_price": result.get("entry_price"),
                    "stop_loss": result.get("stop_loss"),
                    "take_profit": result.get("take_profit"),
                    "atr": result.get("atr"),
                    "reason": result.get("reason", ""),
                })
        return rows

    def _aggregate_by_type(
        self,
        candidates: list[Candidate],
        simulation_rows: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Build per dry-run type metrics."""
        result: dict[str, dict[str, Any]] = {}
        simulation_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in simulation_rows:
            simulation_by_type[str(row.get("dry_run_type", ""))].append(row)

        for dry_run_type in DRY_RUN_SOURCES:
            typed_candidates = [
                candidate
                for candidate in candidates
                if candidate["dry_run_type"] == dry_run_type
            ]
            outcome_metrics = self._outcome_metrics(typed_candidates)
            simulation_metrics = self._simulation_metrics(simulation_by_type[dry_run_type])
            best_symbol, worst_symbol = self._best_worst_symbol(typed_candidates)
            best_horizon = self._best_horizon(outcome_metrics)
            conclusion = self._type_conclusion(
                len(typed_candidates),
                outcome_metrics,
                simulation_metrics,
            )
            result[dry_run_type] = {
                "label": TYPE_LABELS[dry_run_type],
                "candidate_count": len(typed_candidates),
                "best_symbol": best_symbol,
                "worst_symbol": worst_symbol,
                "best_horizon": best_horizon,
                "outcome_metrics": outcome_metrics,
                "trade_simulation": simulation_metrics,
                "conclusion": conclusion,
            }
        return result

    def _overall_metrics(
        self,
        candidates: list[Candidate],
        simulation_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build overall outcome and simulation metrics."""
        outcome_metrics = self._outcome_metrics(candidates)
        simulation_metrics = self._simulation_metrics(simulation_rows)
        best_symbol, worst_symbol = self._best_worst_symbol(candidates)
        best_type = self._best_type(candidates)
        return {
            "total_candidates": len(candidates),
            "best_symbol": best_symbol,
            "worst_symbol": worst_symbol,
            "best_dry_run_type": best_type,
            "outcome_metrics": outcome_metrics,
            "trade_simulation": simulation_metrics,
        }

    def _outcome_metrics(self, candidates: list[Candidate]) -> dict[str, dict[str, Any]]:
        """Aggregate direction-aware returns by horizon."""
        metrics: dict[str, dict[str, Any]] = {}
        for horizon in HORIZONS:
            returns: list[float] = []
            favorable_count = 0
            missing_count = 0
            for candidate in candidates:
                outcome = candidate.get("outcomes", {}).get(horizon, {})
                if not outcome.get("available"):
                    missing_count += 1
                    continue
                direction_return = safe_float(outcome.get("direction_return"))
                returns.append(direction_return)
                if outcome.get("favorable"):
                    favorable_count += 1

            checked = len(returns)
            metrics[horizon] = {
                "checked": checked,
                "missing": missing_count,
                "favorable_count": favorable_count,
                "unfavorable_count": checked - favorable_count,
                "favorable_rate": percent(favorable_count, checked),
                "avg_return": mean(returns),
                "median_return": median_value(returns),
                "max_return": safe_round(max(returns), 4) if returns else None,
                "min_return": safe_round(min(returns), 4) if returns else None,
            }
        return metrics

    def _simulation_metrics(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Aggregate simple TP/SL simulation rows by horizon."""
        metrics: dict[str, dict[str, Any]] = {}
        for horizon in SIMULATION_HORIZONS:
            horizon_rows = [
                row
                for row in rows
                if row.get("horizon") == horizon and row.get("available")
            ]
            r_values = [safe_float(row.get("r_value")) for row in horizon_rows]
            tp_count = sum(1 for row in horizon_rows if row.get("outcome") == "TP_FIRST")
            sl_count = sum(
                1
                for row in horizon_rows
                if row.get("outcome") in {"SL_FIRST", "SL"}
            )
            neither_count = sum(1 for row in horizon_rows if row.get("outcome") == "NEITHER")
            resolved = tp_count + sl_count
            metrics[horizon] = {
                "checked": len(horizon_rows),
                "tp_first": tp_count,
                "sl_first": sl_count,
                "neither": neither_count,
                "estimated_winrate": percent(tp_count, resolved),
                "estimated_profit_factor": self._profit_factor(r_values),
                "estimated_net_R": safe_round(sum(r_values), 4) if r_values else None,
                "average_R": mean(r_values),
            }
        return metrics

    @staticmethod
    def _profit_factor(r_values: list[float]) -> float | None:
        """Calculate Profit Factor from R values without returning Infinity."""
        gains = sum(value for value in r_values if value > 0)
        losses = abs(sum(value for value in r_values if value < 0))
        if not r_values:
            return None
        if losses == 0:
            return round(gains, 4) if gains > 0 else 0.0
        return round(gains / losses, 4)

    @staticmethod
    def _best_horizon(outcome_metrics: dict[str, dict[str, Any]]) -> str | None:
        """Select the horizon with the strongest favorable profile."""
        available = [
            (horizon, metrics)
            for horizon, metrics in outcome_metrics.items()
            if metrics.get("checked", 0) > 0
        ]
        if not available:
            return None
        return max(
            available,
            key=lambda item: (
                item[1].get("favorable_rate", 0),
                item[1].get("avg_return") or -999,
            ),
        )[0]

    @staticmethod
    def _best_worst_symbol(candidates: list[Candidate]) -> tuple[str | None, str | None]:
        """Find best/worst symbols by average 4h direction-aware return."""
        by_symbol: dict[str, list[float]] = defaultdict(list)
        for candidate in candidates:
            outcome = candidate.get("outcomes", {}).get("4h", {})
            if outcome.get("available"):
                by_symbol[candidate["symbol"]].append(
                    safe_float(outcome.get("direction_return"))
                )
        if not by_symbol:
            return None, None
        averages = {
            symbol: sum(values) / len(values)
            for symbol, values in by_symbol.items()
            if values
        }
        if not averages:
            return None, None
        best_symbol = max(averages.items(), key=lambda item: item[1])[0]
        worst_symbol = min(averages.items(), key=lambda item: item[1])[0]
        return best_symbol, worst_symbol

    def _best_type(self, candidates: list[Candidate]) -> str | None:
        """Find the best dry-run type by 4h favorable rate and avg return."""
        grouped: dict[str, list[Candidate]] = defaultdict(list)
        for candidate in candidates:
            grouped[candidate["dry_run_type"]].append(candidate)
        if not grouped:
            return None

        scored: list[tuple[str, float, float, int]] = []
        for dry_run_type, typed_candidates in grouped.items():
            metrics = self._outcome_metrics(typed_candidates).get("4h", {})
            scored.append((
                dry_run_type,
                safe_float(metrics.get("favorable_rate")),
                safe_float(metrics.get("avg_return")),
                int(metrics.get("checked", 0)),
            ))
        valid = [item for item in scored if item[3] > 0]
        if not valid:
            return None
        return max(valid, key=lambda item: (item[1], item[2], item[3]))[0]

    @staticmethod
    def _type_conclusion(
        candidate_count: int,
        outcome_metrics: dict[str, dict[str, Any]],
        simulation_metrics: dict[str, dict[str, Any]],
    ) -> str:
        """Classify one dry-run type."""
        four_hour = outcome_metrics.get("4h", {})
        twenty_four_hour = simulation_metrics.get("24h", {})
        checked_4h = int(four_hour.get("checked", 0))
        favorable_4h = safe_float(four_hour.get("favorable_rate"))
        avg_return_4h = four_hour.get("avg_return")
        pf_24h = twenty_four_hour.get("estimated_profit_factor")

        if candidate_count < 30 or checked_4h < 30:
            return "insufficient_data"
        if favorable_4h < 45 and (avg_return_4h is not None and avg_return_4h < 0):
            return "dangerous"
        if (
            favorable_4h >= 55
            and avg_return_4h is not None
            and avg_return_4h > 0
            and pf_24h is not None
            and pf_24h >= 1.2
        ):
            return "promising"
        return "weak"

    def _determine_status(self, overall: dict[str, Any]) -> str:
        """Determine the global report status."""
        total = int(overall.get("total_candidates", 0))
        if total == 0:
            return "NO_ACTION"
        if total < 30:
            return "INSUFFICIENT_DATA"

        four_hour = overall.get("outcome_metrics", {}).get("4h", {})
        sim_24h = overall.get("trade_simulation", {}).get("24h", {})
        favorable_4h = safe_float(four_hour.get("favorable_rate"))
        avg_return_4h = four_hour.get("avg_return")
        profit_factor = sim_24h.get("estimated_profit_factor")

        if favorable_4h < 45 and avg_return_4h is not None and avg_return_4h < 0:
            return "DANGEROUS"
        if (
            profit_factor is not None
            and profit_factor >= 1.2
            and favorable_4h >= 55
            and avg_return_4h is not None
            and avg_return_4h > 0
        ):
            if total >= 50:
                return "READY_FOR_SHADOW_STRATEGY"
            return "PROMISING_DRY_RUN"
        return "OBSERVE_MORE"

    def _live_strategy_comparison(
        self,
        candidates: list[Candidate],
        simulation_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Compare dry-run observations with live trades in the same period."""
        timestamps = [
            parsed
            for parsed in (parse_dt(candidate.get("timestamp")) for candidate in candidates)
            if parsed is not None
        ]
        start = min(timestamps) if timestamps else None
        end = max(timestamps) if timestamps else None

        live_rows = read_csv_rows(self.base_dir / "trades.csv")
        live_trades = []
        for row in live_rows:
            opened_at = parse_dt(row.get("opened_at"))
            if opened_at is None:
                continue
            if start and end and start <= opened_at <= end:
                live_trades.append(row)

        sim_24h = self._simulation_metrics(simulation_rows).get("24h", {})
        return {
            "period_start": start.isoformat() if start else None,
            "period_end": end.isoformat() if end else None,
            "live_strategy_trades_during_same_period": len(live_trades),
            "dry_run_candidates_count": len(candidates),
            "potential_extra_trades": len(candidates),
            "estimated_winrate": sim_24h.get("estimated_winrate"),
            "estimated_profit_factor": sim_24h.get("estimated_profit_factor"),
            "estimated_net_R": sim_24h.get("estimated_net_R"),
        }

    @staticmethod
    def _recommendation_text(overall: dict[str, Any]) -> str:
        """Build a conservative Russian recommendation text."""
        total = int(overall.get("total_candidates", 0))
        if total == 0:
            return "Кандидатов dry-run пока нет. Live-стратегию не менять."
        if total < 30:
            return "Выборка меньше 30 кандидатов. Нужен дальнейший сбор данных."

        four_hour = overall.get("outcome_metrics", {}).get("4h", {})
        sim_24h = overall.get("trade_simulation", {}).get("24h", {})
        favorable_4h = safe_float(four_hour.get("favorable_rate"))
        avg_return_4h = four_hour.get("avg_return")
        pf_24h = sim_24h.get("estimated_profit_factor")

        if pf_24h is None or pf_24h < 1.2:
            return "Profit Factor ниже защитного порога. Изменения не рекомендованы."
        if favorable_4h < 55:
            return "4h favorable ниже 55%. Продолжать наблюдение."
        if avg_return_4h is None or avg_return_4h <= 0:
            return "Средний 4h return не положительный. Изменения не рекомендованы."
        return "Dry-run выглядит перспективно, но требуется отдельный shadow/backtest этап."

    def _write_candidate_csv(self, candidates: list[Candidate]) -> None:
        """Write enriched candidate rows."""
        fields = [
            "timestamp",
            "dry_run_type",
            "symbol",
            "direction",
            "decision",
            "confidence",
            "weighted_score",
            "edge",
            "edge_gap",
            "filter_name",
            "reason",
            "entry_price",
            "1h_return",
            "1h_favorable",
            "2h_return",
            "2h_favorable",
            "4h_return",
            "4h_favorable",
            "8h_return",
            "8h_favorable",
            "12h_return",
            "12h_favorable",
            "24h_return",
            "24h_favorable",
        ]
        rows: list[dict[str, Any]] = []
        for candidate in candidates:
            row = {
                "timestamp": candidate.get("timestamp"),
                "dry_run_type": candidate.get("dry_run_type"),
                "symbol": candidate.get("symbol"),
                "direction": candidate.get("direction"),
                "decision": candidate.get("decision"),
                "confidence": candidate.get("confidence"),
                "weighted_score": candidate.get("weighted_score"),
                "edge": candidate.get("edge"),
                "edge_gap": candidate.get("edge_gap"),
                "filter_name": candidate.get("filter_name"),
                "reason": candidate.get("reason"),
                "entry_price": "",
            }
            for horizon in HORIZONS:
                outcome = candidate.get("outcomes", {}).get(horizon, {})
                if outcome.get("available") and not row["entry_price"]:
                    row["entry_price"] = outcome.get("entry_price")
                row[f"{horizon}_return"] = outcome.get("direction_return")
                row[f"{horizon}_favorable"] = outcome.get("favorable")
            rows.append(row)
        write_csv(CANDIDATES_CSV_PATH, rows, fields)

    def _write_simulation_csv(self, rows: list[dict[str, Any]]) -> None:
        """Write TP/SL simulation rows."""
        fields = [
            "timestamp",
            "dry_run_type",
            "symbol",
            "direction",
            "horizon",
            "available",
            "outcome",
            "r_value",
            "entry_price",
            "stop_loss",
            "take_profit",
            "atr",
            "reason",
        ]
        write_csv(SIMULATION_CSV_PATH, rows, fields)

    def _write_by_type_csv(self, by_type: dict[str, dict[str, Any]]) -> None:
        """Write compact per-type metrics."""
        fields = [
            "dry_run_type",
            "label",
            "candidate_count",
            "best_symbol",
            "worst_symbol",
            "best_horizon",
            "4h_checked",
            "4h_favorable_rate",
            "4h_avg_return",
            "8h_favorable_rate",
            "24h_favorable_rate",
            "sim_24h_winrate",
            "sim_24h_profit_factor",
            "sim_24h_net_R",
            "conclusion",
        ]
        rows = []
        for dry_run_type, data in by_type.items():
            four_h = data.get("outcome_metrics", {}).get("4h", {})
            eight_h = data.get("outcome_metrics", {}).get("8h", {})
            twenty_four_h = data.get("outcome_metrics", {}).get("24h", {})
            sim_24h = data.get("trade_simulation", {}).get("24h", {})
            rows.append({
                "dry_run_type": dry_run_type,
                "label": data.get("label"),
                "candidate_count": data.get("candidate_count"),
                "best_symbol": data.get("best_symbol"),
                "worst_symbol": data.get("worst_symbol"),
                "best_horizon": data.get("best_horizon"),
                "4h_checked": four_h.get("checked"),
                "4h_favorable_rate": four_h.get("favorable_rate"),
                "4h_avg_return": four_h.get("avg_return"),
                "8h_favorable_rate": eight_h.get("favorable_rate"),
                "24h_favorable_rate": twenty_four_h.get("favorable_rate"),
                "sim_24h_winrate": sim_24h.get("estimated_winrate"),
                "sim_24h_profit_factor": sim_24h.get("estimated_profit_factor"),
                "sim_24h_net_R": sim_24h.get("estimated_net_R"),
                "conclusion": data.get("conclusion"),
            })
        write_csv(BY_TYPE_CSV_PATH, rows, fields)

    def _format_summary(self, report: dict[str, Any]) -> str:
        """Format a short Russian text summary."""
        counts = report.get("summary", {}).get("candidate_counts_by_type", {})
        outcome_4h = report.get("outcome_metrics", {}).get("4h", {})
        outcome_8h = report.get("outcome_metrics", {}).get("8h", {})
        sim_24h = report.get("trade_simulation", {}).get("24h", {})
        best_type = report.get("summary", {}).get("best_dry_run_type")
        best_type_label = TYPE_LABELS.get(str(best_type), "Недостаточно данных")
        best_symbol = report.get("summary", {}).get("best_symbol") or "Недостаточно данных"
        status = report.get("status", "NO_ACTION")
        recommendation = report.get("summary", {}).get("recommendation", "")

        return "\n".join([
            "====================================",
            "Dry-Run Outcome Analyzer v1",
            "====================================",
            f"Статус: {status}",
            f"Кандидатов всего: {report.get('summary', {}).get('total_candidates', 0)}",
            f"Relaxed Edge: {counts.get('relaxed_edge', 0)}",
            f"Long Rebound: {counts.get('long_rebound', 0)}",
            f"ADA Opportunity: {counts.get('ada_opportunity', 0)}",
            f"DOGE/LINK Opportunity: {counts.get('doge_link_opportunity', 0)}",
            f"Лучший dry-run тип: {best_type_label}",
            f"Лучший символ: {best_symbol}",
            f"4h favorable: {outcome_4h.get('favorable_rate', 0.0)}%",
            f"8h favorable: {outcome_8h.get('favorable_rate', 0.0)}%",
            "Trade Simulation:",
            f"Winrate: {sim_24h.get('estimated_winrate', 0.0)}%",
            f"Profit Factor: {sim_24h.get('estimated_profit_factor')}",
            f"Net R: {sim_24h.get('estimated_net_R')}",
            f"Вывод: {recommendation}",
            f"Следующее действие: {self._next_action(status)}",
            "",
            "Файлы:",
            f"- {REPORT_PATH.name}",
            f"- {SUMMARY_PATH.name}",
            f"- {BY_TYPE_CSV_PATH.name}",
            f"- {CANDIDATES_CSV_PATH.name}",
            f"- {SIMULATION_CSV_PATH.name}",
        ])

    @staticmethod
    def _next_action(status: str) -> str:
        if status == "READY_FOR_SHADOW_STRATEGY":
            return "готовить отдельный shadow/backtest, live-логику не менять"
        if status == "PROMISING_DRY_RUN":
            return "продолжить dry-run и подтвердить на большей выборке"
        if status == "DANGEROUS":
            return "не ослаблять стратегию по этим dry-run сигналам"
        if status == "INSUFFICIENT_DATA":
            return "накопить минимум 30 dry-run кандидатов"
        if status == "OBSERVE_MORE":
            return "продолжить наблюдение без изменений стратегии"
        return "ждать появления dry-run кандидатов"


def main() -> None:
    """Run the analyzer from the command line."""
    analyzer = DryRunOutcomeAnalyzer()
    analyzer.print_report()


if __name__ == "__main__":
    main()
