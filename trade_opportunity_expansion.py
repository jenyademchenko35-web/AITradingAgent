"""Trade Opportunity Expansion Framework for AITradingAgent.

This read-only module analyzes where potential trades are lost and whether
careful expansion scenarios would have produced favorable outcomes. It does not
change DecisionEngine, config.py, thresholds, weights, live agent behavior, or
trade execution.
"""

from __future__ import annotations

import csv
import json
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable


BASE_DIR = Path(__file__).resolve().parent
OHLCV_CACHE_DIR = BASE_DIR / "ohlcv_cache"

REPORT_PATH = BASE_DIR / "trade_opportunity_expansion_report.json"
SUMMARY_PATH = BASE_DIR / "trade_opportunity_expansion_summary.txt"
CANDIDATES_CSV_PATH = BASE_DIR / "trade_opportunity_expansion_candidates.csv"
SCENARIOS_CSV_PATH = BASE_DIR / "trade_opportunity_expansion_scenarios.csv"

MIN_EDGE_FALLBACK = 15
MIN_EDGE = MIN_EDGE_FALLBACK
try:
    from config import MIN_EDGE as CONFIG_MIN_EDGE

    MIN_EDGE = int(CONFIG_MIN_EDGE)
except Exception:
    MIN_EDGE = MIN_EDGE_FALLBACK

HORIZONS = {
    "1h": 1,
    "2h": 2,
    "4h": 4,
    "8h": 8,
}

SOURCE_FILES = [
    "signals_v3.csv",
    "decision_debug.csv",
    "decision_diagnostics.csv",
    "decision_explanations.csv",
    "trades.csv",
    "agent_v3_stats.json",
    "strategy_replay_report.json",
]

Candidate = dict[str, Any]
Predicate = Callable[[Candidate], bool]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_dt(value: Any) -> datetime | None:
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
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _percent(part: int | float, total: int | float) -> float:
    return round((float(part) / float(total)) * 100, 2) if total else 0.0


def _avg(values: Iterable[float]) -> float:
    items = list(values)
    return round(sum(items) / len(items), 4) if items else 0.0


class OHLCVCache:
    """Small local OHLCV cache reader for outcome checks."""

    def __init__(self, cache_dir: Path = OHLCV_CACHE_DIR) -> None:
        self.cache_dir = cache_dir
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._timestamps: dict[str, list[datetime]] = {}

    def load_symbol(self, symbol: str) -> list[dict[str, Any]]:
        key = self._symbol_key(symbol)
        if key in self._cache:
            return self._cache[key]
        path = self.cache_dir / f"{key}_1h.csv"
        rows = []
        for row in _read_csv(path):
            ts = _parse_dt(row.get("timestamp"))
            if ts is None:
                continue
            rows.append({
                "timestamp": ts,
                "open": _safe_float(row.get("open")),
                "high": _safe_float(row.get("high")),
                "low": _safe_float(row.get("low")),
                "close": _safe_float(row.get("close")),
                "volume": _safe_float(row.get("volume")),
            })
        rows.sort(key=lambda item: item["timestamp"])
        self._cache[key] = rows
        self._timestamps[key] = [row["timestamp"] for row in rows]
        return rows

    def outcome(self, candidate: Candidate, horizon_hours: int) -> float | None:
        rows = self.load_symbol(str(candidate.get("symbol", "")))
        if not rows:
            return None
        timestamp = _parse_dt(candidate.get("timestamp"))
        if timestamp is None:
            return None
        key = self._symbol_key(str(candidate.get("symbol", "")))
        index = bisect_left(self._timestamps[key], timestamp)
        future_index = index + horizon_hours
        if index >= len(rows) or future_index >= len(rows):
            return None
        entry = rows[index]["close"]
        future = rows[future_index]["close"]
        if entry <= 0:
            return None
        direction = str(candidate.get("direction", "")).upper()
        if direction == "LONG":
            return round(((future - entry) / entry) * 100, 4)
        if direction == "SHORT":
            return round(((entry - future) / entry) * 100, 4)
        return None

    @staticmethod
    def _symbol_key(symbol: str) -> str:
        return str(symbol or "").upper().replace("/", "_")


class TradeOpportunityExpansion:
    """Analyze missed opportunities and safe expansion scenarios."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.ohlcv = OHLCVCache(base_dir / "ohlcv_cache")
        self.warnings: list[str] = []
        self.source_status = self._source_status()

    def build_report(self) -> dict[str, Any]:
        """Build and save the opportunity expansion report."""
        rows = self._load_decision_rows()
        candidates = self._near_setup_candidates(rows)
        candidate_outcomes = self._outcomes_for_candidates(candidates)
        scenarios = self._build_scenarios(rows, candidates, candidate_outcomes)
        report = {
            "generated_at": _utc_now(),
            "status": self._overall_status(scenarios, len(candidates)),
            "mode": "read-only opportunity replay",
            "min_edge_reference": MIN_EDGE,
            "source_files": self.source_status,
            "warnings": self.warnings,
            "opportunity_funnel": self._opportunity_funnel(rows),
            "near_setup": {
                "total_candidates": len(candidates),
                "categories": self._category_counts(candidates),
                "outcome_summary": candidate_outcomes,
            },
            "scenarios": {scenario["scenario"]: scenario for scenario in scenarios},
            "answers": self._answers(rows, candidates, candidate_outcomes, scenarios),
            "restrictions": [
                "DecisionEngine was not changed.",
                "config.py was not changed.",
                "MIN_SCORE, MIN_CONFIDENCE, MIN_EDGE, and weights were not changed.",
                "Live agent was not changed.",
                "No trades were opened or closed.",
                "No recommendation is applied automatically.",
            ],
        }
        self._write_outputs(report, candidates, scenarios)
        return report

    def print_report(self) -> None:
        """Print a compact human-readable report."""
        report = self.build_report()
        print(self._summary_text(report))

    def _source_status(self) -> dict[str, Any]:
        status = {}
        for filename in SOURCE_FILES:
            path = self.base_dir / filename
            exists = path.exists()
            status[filename] = {
                "exists": exists,
                "rows": len(_read_csv(path)) if filename.endswith(".csv") else None,
                "last_modified": (
                    datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
                    if exists
                    else None
                ),
            }
            if not exists:
                self.warnings.append(f"Missing source: {filename}")
        cache_files = list((self.base_dir / "ohlcv_cache").glob("*_1h.csv"))
        status["ohlcv_cache/*.csv"] = {
            "exists": bool(cache_files),
            "rows": None,
            "files": len(cache_files),
        }
        if not cache_files:
            self.warnings.append("Missing OHLCV cache files.")
        return status

    def _load_decision_rows(self) -> list[Candidate]:
        debug_rows = _read_csv(self.base_dir / "decision_debug.csv")
        signal_rows = _read_csv(self.base_dir / "signals_v3.csv")
        source_rows = debug_rows if debug_rows else signal_rows
        decisions = [self._normalize_decision_row(row) for row in source_rows]
        decisions = [row for row in decisions if row.get("timestamp")]
        return decisions

    def _normalize_decision_row(self, row: dict[str, str]) -> Candidate:
        long_total = _safe_float(row.get("long_total"))
        short_total = _safe_float(row.get("short_total"))
        diff = _safe_float(row.get("diff"), abs(long_total - short_total))
        if not row.get("diff"):
            diff = abs(long_total - short_total)
        weighted_score = max(long_total, short_total)
        winner = str(row.get("winner", "")).upper()
        direction = str(row.get("direction", "")).upper()
        if direction not in {"LONG", "SHORT"}:
            if winner in {"LONG", "SHORT"}:
                direction = winner
            elif long_total > short_total:
                direction = "LONG"
            elif short_total > long_total:
                direction = "SHORT"
            else:
                direction = "NEUTRAL"
        return {
            "timestamp": row.get("timestamp", ""),
            "symbol": row.get("symbol", ""),
            "direction": direction,
            "actual_direction": str(row.get("direction", "")).upper(),
            "signal": row.get("signal", ""),
            "score": _safe_float(row.get("score")),
            "confidence": _safe_float(row.get("confidence")),
            "quality": row.get("quality", ""),
            "weighted_score": weighted_score,
            "long_score": long_total,
            "short_score": short_total,
            "diff": diff,
            "winner": winner,
            "reason": row.get("summary", ""),
        }

    def _opportunity_funnel(self, rows: list[Candidate]) -> dict[str, Any]:
        total = len(rows)
        directed = [row for row in rows if row["direction"] in {"LONG", "SHORT"}]
        conf60 = [row for row in directed if row["confidence"] >= 60]
        conf70 = [row for row in directed if row["confidence"] >= 70]
        score15 = [row for row in conf70 if row["weighted_score"] >= 15]
        score20 = [row for row in score15 if row["weighted_score"] >= 20]
        near_edge = [row for row in score20 if row["diff"] >= MIN_EDGE - 3]
        would_watch = [
            row for row in near_edge
            if row["weighted_score"] >= 23 and row["confidence"] >= 70
        ]
        would_setup = [
            row for row in would_watch
            if row["weighted_score"] >= 25 and row["confidence"] >= 80
        ]
        actual_setup = [
            row for row in would_setup
            if row["signal"] in {"SETUP", "HIGH PRIORITY"}
        ]
        stages = [
            ("total_analyses", rows),
            ("has_direction_long_short", directed),
            ("confidence_ge_60", conf60),
            ("confidence_ge_70", conf70),
            ("weighted_score_ge_15", score15),
            ("weighted_score_ge_20", score20),
            ("diff_near_min_edge", near_edge),
            ("would_be_watch", would_watch),
            ("would_be_setup", would_setup),
            ("actual_setup", actual_setup),
        ]
        return {
            name: {
                "count": len(items),
                "percent_of_total": _percent(len(items), total),
                "drop_from_previous": (
                    len(stages[index - 1][1]) - len(items)
                    if index > 0 else 0
                ),
            }
            for index, (name, items) in enumerate(stages)
        }

    def _near_setup_candidates(self, rows: list[Candidate]) -> list[Candidate]:
        candidates = []
        for row in rows:
            if row["score"] != 0:
                continue
            if row["confidence"] < 60:
                continue
            if row["weighted_score"] < 18:
                continue
            distance = MIN_EDGE - row["diff"]
            if distance <= 1:
                category = "VERY_CLOSE"
            elif distance <= 3:
                category = "CLOSE"
            elif distance <= 6:
                category = "MEDIUM"
            else:
                category = "FAR"
            candidate = dict(row)
            candidate["near_setup_category"] = category
            candidates.append(candidate)
        return candidates

    def _outcomes_for_candidates(self, candidates: list[Candidate]) -> dict[str, Any]:
        return self._outcome_summary(candidates)

    def _outcome_summary(self, candidates: list[Candidate]) -> dict[str, Any]:
        horizons: dict[str, Any] = {}
        symbol_stats: dict[str, list[float]] = {}
        for label, hours in HORIZONS.items():
            returns = []
            favorable = 0
            checked = 0
            symbol_returns: dict[str, list[float]] = {}
            for candidate in candidates:
                result = self.ohlcv.outcome(candidate, hours)
                if result is None:
                    continue
                checked += 1
                returns.append(result)
                if result > 0:
                    favorable += 1
                symbol_returns.setdefault(candidate["symbol"], []).append(result)
                if label == "4h":
                    symbol_stats.setdefault(candidate["symbol"], []).append(result)

            best_symbol, worst_symbol = self._best_worst_symbols(symbol_returns)
            horizons[label] = {
                "checked": checked,
                "favorable": favorable,
                "favorable_percent": _percent(favorable, checked),
                "avg_return_percent": _avg(returns),
                "median_return_percent": round(median(returns), 4) if returns else 0.0,
                "best_symbol": best_symbol,
                "worst_symbol": worst_symbol,
            }
        best_symbol, worst_symbol = self._best_worst_symbols(symbol_stats)
        return {
            "horizons": horizons,
            "best_symbol_4h": best_symbol,
            "worst_symbol_4h": worst_symbol,
        }

    @staticmethod
    def _best_worst_symbols(symbol_returns: dict[str, list[float]]) -> tuple[str | None, str | None]:
        if not symbol_returns:
            return None, None
        averages = {
            symbol: _avg(values)
            for symbol, values in symbol_returns.items()
            if values
        }
        if not averages:
            return None, None
        return max(averages, key=averages.get), min(averages, key=averages.get)

    def _build_scenarios(
        self,
        rows: list[Candidate],
        near_candidates: list[Candidate],
        near_outcomes: dict[str, Any],
    ) -> list[dict[str, Any]]:
        best_symbol = near_outcomes.get("best_symbol_4h")
        definitions: list[tuple[str, str, list[Candidate]]] = [
            ("baseline", "Actual SETUP/HIGH PRIORITY only.", [
                row for row in rows if row["signal"] in {"SETUP", "HIGH PRIORITY"}
            ]),
            ("confidence_70", "Near candidates with confidence >= 70 and weighted score >= 23.", [
                row for row in near_candidates if row["confidence"] >= 70 and row["weighted_score"] >= 23
            ]),
            ("confidence_65", "Near candidates with confidence >= 65 and weighted score >= 23.", [
                row for row in near_candidates if row["confidence"] >= 65 and row["weighted_score"] >= 23
            ]),
            ("min_score_23", "Near candidates with weighted score >= 23 and confidence >= 70.", [
                row for row in near_candidates if row["weighted_score"] >= 23 and row["confidence"] >= 70
            ]),
            ("min_score_22", "Near candidates with weighted score >= 22 and confidence >= 70.", [
                row for row in near_candidates if row["weighted_score"] >= 22 and row["confidence"] >= 70
            ]),
            ("min_edge_minus_2", "Near candidates with diff >= MIN_EDGE - 2 and weighted score >= 20.", [
                row for row in near_candidates if row["diff"] >= MIN_EDGE - 2 and row["weighted_score"] >= 20
            ]),
            ("min_edge_minus_3", "Near candidates with diff >= MIN_EDGE - 3 and weighted score >= 20.", [
                row for row in near_candidates if row["diff"] >= MIN_EDGE - 3 and row["weighted_score"] >= 20
            ]),
            ("watch_to_setup_if_edge_close", "WATCH or near candidates close to edge.", [
                row for row in rows
                if (
                    row["signal"] == "WATCH"
                    or (
                        row["score"] == 0
                        and row["weighted_score"] >= 23
                        and row["confidence"] >= 70
                        and row["diff"] >= MIN_EDGE - 2
                    )
                )
            ]),
            ("symbol_specific_best_only", f"Near candidates only for best 4h symbol: {best_symbol}.", [
                row for row in near_candidates
                if best_symbol and row["symbol"] == best_symbol and row["confidence"] >= 60 and row["weighted_score"] >= 18
            ]),
        ]
        return [
            self._scenario_payload(name, description, candidates)
            for name, description, candidates in definitions
        ]

    def _scenario_payload(
        self,
        name: str,
        description: str,
        candidates: list[Candidate],
    ) -> dict[str, Any]:
        outcomes = self._outcome_summary(candidates)
        h1 = outcomes["horizons"].get("1h", {})
        h4 = outcomes["horizons"].get("4h", {})
        h8 = outcomes["horizons"].get("8h", {})
        sample_size = h4.get("checked", 0)
        status, risk_warning = self._quality_gate(sample_size, h4, h8)
        return {
            "scenario": name,
            "description": description,
            "candidate_count": len(candidates),
            "estimated_favorable_1h": h1.get("favorable_percent", 0.0),
            "estimated_favorable_4h": h4.get("favorable_percent", 0.0),
            "estimated_favorable_8h": h8.get("favorable_percent", 0.0),
            "avg_return_4h": h4.get("avg_return_percent", 0.0),
            "avg_return_8h": h8.get("avg_return_percent", 0.0),
            "sample_size": sample_size,
            "risk_warning": risk_warning,
            "status": status,
            "outcomes": outcomes,
        }

    @staticmethod
    def _quality_gate(
        sample_size: int,
        h4: dict[str, Any],
        h8: dict[str, Any],
    ) -> tuple[str, str]:
        warnings = []
        avg4 = _safe_float(h4.get("avg_return_percent"))
        avg8 = _safe_float(h8.get("avg_return_percent"))
        fav4 = _safe_float(h4.get("favorable_percent"))
        if sample_size < 100:
            warnings.append("sample_size < 100")
        if avg4 <= 0:
            warnings.append("avg_return_4h <= 0")
        if avg8 <= 0:
            warnings.append("avg_return_8h <= 0")
        if fav4 < 52:
            warnings.append("favorable_4h < 52%")
        if warnings:
            return "OBSERVE_ONLY", "; ".join(warnings)
        if sample_size >= 250:
            return "READY_FOR_BACKTEST", "Quality gate passed with larger sample."
        return "PROMISING_DRY_RUN", "Quality gate passed; collect more dry-run evidence."

    @staticmethod
    def _category_counts(candidates: list[Candidate]) -> dict[str, int]:
        counts = {"VERY_CLOSE": 0, "CLOSE": 0, "MEDIUM": 0, "FAR": 0}
        for candidate in candidates:
            category = candidate.get("near_setup_category", "FAR")
            counts[category] = counts.get(category, 0) + 1
        return counts

    @staticmethod
    def _overall_status(scenarios: list[dict[str, Any]], candidates_count: int) -> str:
        if candidates_count == 0:
            return "INSUFFICIENT_DATA"
        if any(scenario["status"] == "READY_FOR_BACKTEST" for scenario in scenarios):
            return "READY_FOR_BACKTEST"
        if any(scenario["status"] == "PROMISING_DRY_RUN" for scenario in scenarios):
            return "PROMISING_DRY_RUN"
        return "OBSERVE_ONLY"

    def _answers(
        self,
        rows: list[Candidate],
        candidates: list[Candidate],
        candidate_outcomes: dict[str, Any],
        scenarios: list[dict[str, Any]],
    ) -> dict[str, Any]:
        funnel = self._opportunity_funnel(rows)
        largest_drop = self._largest_funnel_drop(funnel)
        promising = [
            scenario["scenario"]
            for scenario in scenarios
            if scenario["status"] in {"PROMISING_DRY_RUN", "READY_FOR_BACKTEST"}
        ]
        ready = [
            scenario["scenario"]
            for scenario in scenarios
            if scenario["status"] == "READY_FOR_BACKTEST"
        ]
        dangerous = [
            scenario["scenario"]
            for scenario in scenarios
            if "avg_return_4h <= 0" in scenario["risk_warning"]
            or "avg_return_8h <= 0" in scenario["risk_warning"]
        ]
        h4 = candidate_outcomes["horizons"].get("4h", {})
        h8 = candidate_outcomes["horizons"].get("8h", {})
        return {
            "why_no_trades": (
                "Most candidates fail before actual SETUP because directional edge stays below MIN_EDGE "
                "or weighted score/confidence does not pass setup-quality thresholds."
            ),
            "where_candidates_are_lost": largest_drop,
            "missed_good_trades": (
                "Yes, if 4h/8h near-setup returns are positive and favorable rate is above 52%; "
                "otherwise not proven."
            ),
            "near_setup_4h_favorable": h4.get("favorable_percent", 0.0),
            "near_setup_4h_avg_return": h4.get("avg_return_percent", 0.0),
            "near_setup_8h_avg_return": h8.get("avg_return_percent", 0.0),
            "can_safely_expand_now": bool(ready),
            "promising_scenarios": promising,
            "ready_for_backtest_scenarios": ready,
            "dangerous_scenarios": dangerous,
            "final_note": "No live strategy changes are recommended automatically.",
        }

    @staticmethod
    def _largest_funnel_drop(funnel: dict[str, Any]) -> dict[str, Any]:
        stage = max(
            funnel.items(),
            key=lambda item: item[1].get("drop_from_previous", 0),
        )
        return {
            "stage": stage[0],
            "drop_from_previous": stage[1].get("drop_from_previous", 0),
            "remaining": stage[1].get("count", 0),
        }

    def _write_outputs(
        self,
        report: dict[str, Any],
        candidates: list[Candidate],
        scenarios: list[dict[str, Any]],
    ) -> None:
        with REPORT_PATH.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
        with SUMMARY_PATH.open("w", encoding="utf-8") as handle:
            handle.write(self._summary_text(report))
        self._write_candidates_csv(candidates)
        self._write_scenarios_csv(scenarios)

    @staticmethod
    def _write_candidates_csv(candidates: list[Candidate]) -> None:
        fieldnames = [
            "timestamp",
            "symbol",
            "direction",
            "confidence",
            "weighted_score",
            "long_score",
            "short_score",
            "diff",
            "reason",
            "near_setup_category",
        ]
        with CANDIDATES_CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for candidate in candidates:
                writer.writerow({field: candidate.get(field, "") for field in fieldnames})

    @staticmethod
    def _write_scenarios_csv(scenarios: list[dict[str, Any]]) -> None:
        fieldnames = [
            "scenario",
            "candidate_count",
            "estimated_favorable_1h",
            "estimated_favorable_4h",
            "estimated_favorable_8h",
            "avg_return_4h",
            "avg_return_8h",
            "sample_size",
            "risk_warning",
            "status",
        ]
        with SCENARIOS_CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for scenario in scenarios:
                writer.writerow({field: scenario.get(field, "") for field in fieldnames})

    @staticmethod
    def _summary_text(report: dict[str, Any]) -> str:
        answers = report["answers"]
        near = report["near_setup"]
        lines = [
            "Trade Opportunity Expansion",
            "===========================",
            f"Generated: {report['generated_at']}",
            f"Status: {report['status']}",
            f"MIN_EDGE reference: {report['min_edge_reference']}",
            "",
            "Opportunity Funnel:",
        ]
        for stage, payload in report["opportunity_funnel"].items():
            lines.append(
                f"- {stage}: {payload['count']} "
                f"({payload['percent_of_total']}%, drop={payload['drop_from_previous']})"
            )
        lines.extend([
            "",
            "Near-Setup:",
            f"- Candidates: {near['total_candidates']}",
            f"- Categories: {near['categories']}",
            f"- 4h favorable: {answers['near_setup_4h_favorable']}%",
            f"- 4h avg return: {answers['near_setup_4h_avg_return']}%",
            f"- 8h avg return: {answers['near_setup_8h_avg_return']}%",
            "",
            "Scenario Summary:",
        ])
        for scenario in report["scenarios"].values():
            lines.append(
                f"- {scenario['scenario']}: count={scenario['candidate_count']}, "
                f"4h fav={scenario['estimated_favorable_4h']}%, "
                f"4h avg={scenario['avg_return_4h']}%, "
                f"8h avg={scenario['avg_return_8h']}%, "
                f"status={scenario['status']}"
            )
        lines.extend([
            "",
            "Answers:",
            f"- Why no trades: {answers['why_no_trades']}",
            f"- Where lost: {answers['where_candidates_are_lost']}",
            f"- Good missed trades: {answers['missed_good_trades']}",
            f"- Can safely expand now: {answers['can_safely_expand_now']}",
            f"- Promising scenarios: {answers['promising_scenarios'] or 'none'}",
            f"- Ready for backtest: {answers['ready_for_backtest_scenarios'] or 'none'}",
            f"- Dangerous scenarios: {answers['dangerous_scenarios'] or 'none'}",
            f"- Final note: {answers['final_note']}",
            "",
            "Files:",
            f"- {REPORT_PATH.name}",
            f"- {SUMMARY_PATH.name}",
            f"- {CANDIDATES_CSV_PATH.name}",
            f"- {SCENARIOS_CSV_PATH.name}",
        ])
        return "\n".join(lines) + "\n"


def main() -> None:
    """CLI entrypoint."""
    framework = TradeOpportunityExpansion()
    framework.print_report()


if __name__ == "__main__":
    main()
