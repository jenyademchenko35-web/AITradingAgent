"""Decision pipeline profiler for AITradingAgent.

This read-only module explains where recent symbols drop out of the decision
pipeline and why Score=0 / NO TRADE dominates. It reads CSV/JSON artifacts only
and does not modify DecisionEngine, config.py, thresholds, weights, live agent,
or trading logic.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


BASE_DIR = Path(__file__).resolve().parent

REPORT_PATH = BASE_DIR / "decision_pipeline_profile_report.json"
SUMMARY_PATH = BASE_DIR / "decision_pipeline_profile_summary.txt"
BY_SYMBOL_CSV_PATH = BASE_DIR / "decision_pipeline_profile_by_symbol.csv"

RECENT_WINDOW = 90
MIN_EDGE_FALLBACK = 15
MIN_SCORE_FALLBACK = 25
MIN_EDGE = MIN_EDGE_FALLBACK
MIN_SCORE = MIN_SCORE_FALLBACK
try:
    from config import MIN_EDGE as CONFIG_MIN_EDGE
    from config import MIN_SCORE as CONFIG_MIN_SCORE

    MIN_EDGE = int(CONFIG_MIN_EDGE)
    MIN_SCORE = int(CONFIG_MIN_SCORE)
except Exception:
    pass

SOURCE_FILES = [
    "signals_v3.csv",
    "decision_debug.csv",
    "decision_diagnostics.csv",
    "decision_explanations.csv",
    "agent_v3_stats.json",
]

Row = dict[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


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


def _avg(values: Iterable[Any]) -> float:
    numbers = [_safe_float(value) for value in values]
    return round(mean(numbers), 4) if numbers else 0.0


class DecisionPipelineProfiler:
    """Profile recent and historical decision pipeline drop-offs."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.warnings: list[str] = []
        self.source_status = self._source_status()

    def build_report(self) -> dict[str, Any]:
        """Build and save the pipeline profile report."""
        rows = self._load_decision_rows()
        diagnostics = self._load_diagnostics()
        explanations = self._load_explanations()
        recent = rows[-RECENT_WINDOW:]
        recent_diagnostics = diagnostics[-RECENT_WINDOW:]
        recent_explanations = explanations[-RECENT_WINDOW:]
        by_symbol = self._by_symbol_profile(recent, recent_diagnostics)
        report = {
            "generated_at": _utc_now(),
            "status": self._status(recent),
            "mode": "read-only pipeline profiling",
            "min_edge_reference": MIN_EDGE,
            "min_score_reference": MIN_SCORE,
            "recent_window": len(recent),
            "source_files": self.source_status,
            "warnings": self.warnings,
            "agent_stats": _read_json(self.base_dir / "agent_v3_stats.json"),
            "pipeline_funnel": self._pipeline_funnel(recent),
            "overall_pipeline_funnel": self._pipeline_funnel(rows),
            "score_zero_analysis": self._score_zero_analysis(recent, recent_diagnostics, recent_explanations),
            "blocker_analysis": self._blocker_analysis(recent_diagnostics),
            "closest_symbols": self._closest_symbols(by_symbol),
            "near_setup_candidates": self._near_setup_candidates(recent),
            "symbol_explanations": self._symbol_explanations(recent, recent_diagnostics, recent_explanations),
            "by_symbol": by_symbol,
            "answers": self._answers(recent, by_symbol, recent_diagnostics),
            "restrictions": [
                "DecisionEngine was not changed.",
                "config.py was not changed.",
                "MIN_SCORE, MIN_EDGE, and weights were not changed.",
                "multi_timeframe_agent_v3.py was not changed.",
                "Live logic was not changed.",
            ],
        }
        self._write_outputs(report)
        return report

    def print_report(self) -> None:
        """Print a compact profiler summary."""
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
        return status

    def _load_decision_rows(self) -> list[Row]:
        debug_rows = _read_csv(self.base_dir / "decision_debug.csv")
        signal_rows = _read_csv(self.base_dir / "signals_v3.csv")
        source_rows = debug_rows if debug_rows else signal_rows
        return [self._normalize_row(row) for row in source_rows]

    def _load_diagnostics(self) -> list[Row]:
        return [dict(row) for row in _read_csv(self.base_dir / "decision_diagnostics.csv")]

    def _load_explanations(self) -> list[Row]:
        return [dict(row) for row in _read_csv(self.base_dir / "decision_explanations.csv")]

    def _normalize_row(self, row: dict[str, str]) -> Row:
        long_score = _safe_float(row.get("long_total"))
        short_score = _safe_float(row.get("short_total"))
        weighted_score = max(long_score, short_score)
        diff = _safe_float(row.get("diff"), abs(long_score - short_score))
        if not row.get("diff"):
            diff = abs(long_score - short_score)
        direction = str(row.get("direction", "")).upper()
        winner = str(row.get("winner", "")).upper()
        candidate_direction = direction
        if candidate_direction not in {"LONG", "SHORT"}:
            if winner in {"LONG", "SHORT"}:
                candidate_direction = winner
            elif long_score > short_score:
                candidate_direction = "LONG"
            elif short_score > long_score:
                candidate_direction = "SHORT"
            else:
                candidate_direction = "NEUTRAL"
        return {
            "timestamp": row.get("timestamp", ""),
            "symbol": row.get("symbol", ""),
            "direction": direction,
            "candidate_direction": candidate_direction,
            "signal": row.get("signal", ""),
            "score": _safe_float(row.get("score")),
            "confidence": _safe_float(row.get("confidence")),
            "quality": row.get("quality", ""),
            "long_score": long_score,
            "short_score": short_score,
            "weighted_score": weighted_score,
            "diff": diff,
            "summary": row.get("summary", ""),
        }

    def _pipeline_funnel(self, rows: list[Row]) -> dict[str, Any]:
        total = len(rows)
        stage_rows: list[tuple[str, list[Row]]] = []
        current = rows
        stage_rows.append(("total_analyzed", current))
        current = [row for row in current if row["confidence"] >= 60]
        stage_rows.append(("confidence_ge_60", current))
        current = [row for row in current if row["confidence"] >= 70]
        stage_rows.append(("confidence_ge_70", current))
        current = [row for row in current if row["weighted_score"] >= 15]
        stage_rows.append(("weighted_score_ge_15", current))
        current = [row for row in current if row["weighted_score"] >= 20]
        stage_rows.append(("weighted_score_ge_20", current))
        current = [row for row in current if row["diff"] >= MIN_EDGE]
        stage_rows.append(("directional_edge_pass", current))
        current = [row for row in current if row["weighted_score"] >= MIN_SCORE]
        stage_rows.append(("min_score_pass", current))

        funnel = {}
        previous_symbols: Counter[str] | None = None
        previous_count = None
        for stage, items in stage_rows:
            current_symbols = Counter(row["symbol"] for row in items)
            lost_symbols = {}
            if previous_symbols is not None:
                lost = previous_symbols - current_symbols
                lost_symbols = dict(lost.most_common(5))
            count = len(items)
            drop = (previous_count - count) if previous_count is not None else 0
            funnel[stage] = {
                "count": count,
                "percent": _percent(count, total),
                "drop_count": drop,
                "drop_percent": _percent(drop, previous_count or total),
                "top_symbols_lost": lost_symbols,
                "average_confidence": _avg(row["confidence"] for row in items),
                "average_score": _avg(row["score"] for row in items),
                "average_diff": _avg(row["diff"] for row in items),
            }
            previous_symbols = current_symbols
            previous_count = count

        final_distribution = Counter(row["signal"] for row in rows)
        funnel["final_distribution"] = {
            signal: {
                "count": count,
                "percent": _percent(count, total),
            }
            for signal, count in final_distribution.items()
        }
        return funnel

    def _score_zero_analysis(
        self,
        rows: list[Row],
        diagnostics: list[Row],
        explanations: list[Row],
    ) -> dict[str, Any]:
        score_zero = [row for row in rows if row["score"] == 0]
        edge_fail = [row for row in score_zero if row["diff"] < MIN_EDGE]
        weighted_20 = [row for row in score_zero if row["weighted_score"] >= 20]
        high_conf = [row for row in score_zero if row["confidence"] >= 70]
        explanation_reasons = Counter()
        for row in explanations:
            if str(row.get("score", "")) == "0":
                summary = row.get("summary") or row.get("reasons") or ""
                explanation_reasons[summary] += 1
        return {
            "score_zero_count": len(score_zero),
            "score_zero_percent": _percent(len(score_zero), len(rows)),
            "edge_fail_count": len(edge_fail),
            "edge_fail_percent_of_score_zero": _percent(len(edge_fail), len(score_zero)),
            "weighted_score_ge_20_count": len(weighted_20),
            "confidence_ge_70_count": len(high_conf),
            "average_confidence": _avg(row["confidence"] for row in score_zero),
            "average_weighted_score": _avg(row["weighted_score"] for row in score_zero),
            "average_diff": _avg(row["diff"] for row in score_zero),
            "top_explanation_summaries": dict(explanation_reasons.most_common(5)),
            "decision_engine_reason": (
                "Score=0 occurs when directional diff is below MIN_EDGE; confidence can still be "
                "moderate/high because it is derived from diff before the edge gate zeroes score."
            ),
        }

    @staticmethod
    def _blocker_analysis(diagnostics: list[Row]) -> dict[str, Any]:
        blockers = Counter(row.get("primary_blocker", "") or "UNKNOWN" for row in diagnostics)
        return {
            "main_blocker": blockers.most_common(1)[0][0] if blockers else "UNKNOWN",
            "blockers": dict(blockers.most_common()),
        }

    def _by_symbol_profile(self, rows: list[Row], diagnostics: list[Row]) -> dict[str, Any]:
        diagnostics_by_symbol: dict[str, list[Row]] = defaultdict(list)
        for row in diagnostics:
            diagnostics_by_symbol[row.get("symbol", "")].append(row)

        by_symbol: dict[str, Any] = {}
        for symbol in sorted({row["symbol"] for row in rows}):
            items = [row for row in rows if row["symbol"] == symbol]
            score_zero = [row for row in items if row["score"] == 0]
            near_setup = [row for row in items if self._is_near_setup(row)]
            blockers = Counter(
                row.get("primary_blocker", "") or "UNKNOWN"
                for row in diagnostics_by_symbol.get(symbol, [])
            )
            latest = items[-1] if items else {}
            by_symbol[symbol] = {
                "rows": len(items),
                "no_trade_count": sum(1 for row in items if row["signal"] == "NO TRADE"),
                "score_zero_count": len(score_zero),
                "near_setup_count": len(near_setup),
                "average_confidence": _avg(row["confidence"] for row in items),
                "average_score": _avg(row["score"] for row in items),
                "average_weighted_score": _avg(row["weighted_score"] for row in items),
                "average_diff": _avg(row["diff"] for row in items),
                "max_weighted_score": max((row["weighted_score"] for row in items), default=0.0),
                "max_diff": max((row["diff"] for row in items), default=0.0),
                "primary_blocker": blockers.most_common(1)[0][0] if blockers else "UNKNOWN",
                "latest_confidence": latest.get("confidence", 0.0),
                "latest_score": latest.get("score", 0.0),
                "latest_weighted_score": latest.get("weighted_score", 0.0),
                "latest_diff": latest.get("diff", 0.0),
                "latest_signal": latest.get("signal", ""),
            }
        return by_symbol

    @staticmethod
    def _closest_symbols(by_symbol: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for symbol, payload in by_symbol.items():
            distance = max(0.0, MIN_EDGE - payload["max_diff"])
            rows.append({
                "symbol": symbol,
                "near_setup_count": payload["near_setup_count"],
                "max_diff": payload["max_diff"],
                "distance_to_min_edge": distance,
                "max_weighted_score": payload["max_weighted_score"],
                "primary_blocker": payload["primary_blocker"],
            })
        rows.sort(key=lambda item: (item["distance_to_min_edge"], -item["near_setup_count"]))
        return rows[:9]

    def _near_setup_candidates(self, rows: list[Row]) -> dict[str, Any]:
        candidates = [row for row in rows if self._is_near_setup(row)]
        categories = Counter(self._near_setup_category(row) for row in candidates)
        return {
            "count": len(candidates),
            "categories": dict(categories),
            "examples": [
                {
                    "timestamp": row["timestamp"],
                    "symbol": row["symbol"],
                    "candidate_direction": row["candidate_direction"],
                    "confidence": row["confidence"],
                    "weighted_score": row["weighted_score"],
                    "diff": row["diff"],
                    "category": self._near_setup_category(row),
                }
                for row in sorted(candidates, key=lambda item: (MIN_EDGE - item["diff"], -item["weighted_score"]))[:10]
            ],
        }

    @staticmethod
    def _is_near_setup(row: Row) -> bool:
        return (
            row["score"] == 0
            and row["confidence"] >= 60
            and row["weighted_score"] >= 18
            and 0 <= (MIN_EDGE - row["diff"]) <= 6
        )

    @staticmethod
    def _near_setup_category(row: Row) -> str:
        distance = MIN_EDGE - row["diff"]
        if distance <= 1:
            return "VERY_CLOSE"
        if distance <= 3:
            return "CLOSE"
        if distance <= 6:
            return "MEDIUM"
        return "FAR"

    def _symbol_explanations(
        self,
        rows: list[Row],
        diagnostics: list[Row],
        explanations: list[Row],
    ) -> dict[str, Any]:
        result = {}
        diagnostics_by_symbol = {row.get("symbol"): row for row in diagnostics}
        explanations_by_symbol = {row.get("symbol"): row for row in explanations}
        for symbol in ("LINK/USDT", "DOGE/USDT"):
            items = [row for row in rows if row["symbol"] == symbol]
            if not items:
                continue
            latest = items[-1]
            diagnostic = diagnostics_by_symbol.get(symbol, {})
            explanation = explanations_by_symbol.get(symbol, {})
            result[symbol] = {
                "latest_confidence": latest["confidence"],
                "latest_score": latest["score"],
                "weighted_score": latest["weighted_score"],
                "diff": latest["diff"],
                "distance_to_min_edge": max(0.0, MIN_EDGE - latest["diff"]),
                "primary_blocker": diagnostic.get("primary_blocker", "UNKNOWN"),
                "failed_filters": explanation.get("failed", ""),
                "reason": (
                    f"{symbol} has confidence {latest['confidence']} because diff={latest['diff']} "
                    f"still creates directional confidence, but diff is below MIN_EDGE={MIN_EDGE}; "
                    "DecisionEngine therefore sets score to 0."
                ),
            }
        return result

    def _answers(
        self,
        recent: list[Row],
        by_symbol: dict[str, Any],
        diagnostics: list[Row],
    ) -> dict[str, Any]:
        funnel = self._pipeline_funnel(recent)
        largest_drop = max(
            (
                (stage, payload)
                for stage, payload in funnel.items()
                if isinstance(payload, dict) and "drop_count" in payload
            ),
            key=lambda item: item[1]["drop_count"],
        )
        blockers = self._blocker_analysis(diagnostics)
        final_dist = funnel.get("final_distribution", {})
        return {
            "why_all_no_trade": (
                "Recent symbols mostly have enough raw weighted score, but the directional edge "
                "does not pass MIN_EDGE and failed Structure/Risk filters dominate explanations."
            ),
            "largest_pipeline_drop": {
                "stage": largest_drop[0],
                "drop_count": largest_drop[1]["drop_count"],
                "drop_percent": largest_drop[1]["drop_percent"],
            },
            "main_blocker": blockers["main_blocker"],
            "final_distribution": final_dist,
            "near_setup_exists": any(payload["near_setup_count"] > 0 for payload in by_symbol.values()),
            "closest_symbols": self._closest_symbols(by_symbol)[:5],
        }

    @staticmethod
    def _status(recent: list[Row]) -> str:
        if not recent:
            return "INSUFFICIENT_DATA"
        setup_count = sum(1 for row in recent if row["signal"] in {"SETUP", "HIGH PRIORITY"})
        near_count = sum(1 for row in recent if DecisionPipelineProfiler._is_near_setup(row))
        if setup_count == 0 and near_count > 0:
            return "PROMISING_DRY_RUN"
        if setup_count == 0:
            return "OBSERVE_ONLY"
        return "NO_ACTION"

    def _write_outputs(self, report: dict[str, Any]) -> None:
        with REPORT_PATH.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
        with SUMMARY_PATH.open("w", encoding="utf-8") as handle:
            handle.write(self._summary_text(report))
        self._write_by_symbol_csv(report["by_symbol"])

    @staticmethod
    def _write_by_symbol_csv(by_symbol: dict[str, Any]) -> None:
        fieldnames = [
            "symbol",
            "rows",
            "no_trade_count",
            "score_zero_count",
            "near_setup_count",
            "average_confidence",
            "average_score",
            "average_weighted_score",
            "average_diff",
            "max_weighted_score",
            "max_diff",
            "primary_blocker",
            "latest_confidence",
            "latest_score",
            "latest_weighted_score",
            "latest_diff",
            "latest_signal",
        ]
        with BY_SYMBOL_CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for symbol, payload in sorted(by_symbol.items()):
                row = {"symbol": symbol}
                row.update(payload)
                writer.writerow({field: row.get(field, "") for field in fieldnames})

    @staticmethod
    def _summary_text(report: dict[str, Any]) -> str:
        answers = report["answers"]
        score_zero = report["score_zero_analysis"]
        lines = [
            "Decision Pipeline Profile",
            "=========================",
            f"Generated: {report['generated_at']}",
            f"Status: {report['status']}",
            f"Recent window: {report['recent_window']}",
            f"MIN_EDGE: {report['min_edge_reference']}",
            f"MIN_SCORE: {report['min_score_reference']}",
            "",
            "Pipeline Funnel:",
        ]
        for stage, payload in report["pipeline_funnel"].items():
            if stage == "final_distribution":
                continue
            lines.append(
                f"- {stage}: count={payload['count']} ({payload['percent']}%), "
                f"drop={payload['drop_count']} ({payload['drop_percent']}%), "
                f"avg_conf={payload['average_confidence']}, avg_diff={payload['average_diff']}"
            )
        lines.extend([
            "",
            "Final Distribution:",
            f"- {report['pipeline_funnel'].get('final_distribution', {})}",
            "",
            "Score=0:",
            f"- Count: {score_zero['score_zero_count']} ({score_zero['score_zero_percent']}%)",
            f"- Edge fail: {score_zero['edge_fail_count']} ({score_zero['edge_fail_percent_of_score_zero']}%)",
            f"- Weighted score >=20: {score_zero['weighted_score_ge_20_count']}",
            f"- Confidence >=70: {score_zero['confidence_ge_70_count']}",
            f"- Avg confidence: {score_zero['average_confidence']}",
            f"- Avg diff: {score_zero['average_diff']}",
            "",
            "Blockers:",
            f"- Main blocker: {report['blocker_analysis']['main_blocker']}",
            f"- Distribution: {report['blocker_analysis']['blockers']}",
            "",
            "Closest Symbols:",
        ])
        for item in report["closest_symbols"][:5]:
            lines.append(
                f"- {item['symbol']}: near={item['near_setup_count']}, "
                f"max_diff={item['max_diff']}, distance={item['distance_to_min_edge']}, "
                f"blocker={item['primary_blocker']}"
            )
        lines.extend([
            "",
            "LINK/DOGE:",
            json.dumps(report["symbol_explanations"], ensure_ascii=False, indent=2),
            "",
            "Answers:",
            f"- Why all NO TRADE: {answers['why_all_no_trade']}",
            f"- Largest drop: {answers['largest_pipeline_drop']}",
            f"- Near setup exists: {answers['near_setup_exists']}",
            "",
            "Files:",
            f"- {REPORT_PATH.name}",
            f"- {SUMMARY_PATH.name}",
            f"- {BY_SYMBOL_CSV_PATH.name}",
        ])
        return "\n".join(lines) + "\n"


def main() -> None:
    """CLI entrypoint."""
    profiler = DecisionPipelineProfiler()
    profiler.print_report()


if __name__ == "__main__":
    main()
