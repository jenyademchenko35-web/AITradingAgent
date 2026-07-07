"""Dry-run pipeline profiler for AITradingAgent.

This read-only module explains why dry-run candidate logs are empty or sparse.
It profiles the filtering chain for each dry-run rule using already logged
decision data. It never changes DecisionEngine, config.py, thresholds, weights,
the live agent, or trade execution.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Iterable


BASE_DIR = Path(__file__).resolve().parent

REPORT_PATH = BASE_DIR / "dry_run_pipeline_report.json"
SUMMARY_PATH = BASE_DIR / "dry_run_pipeline_summary.txt"
RULES_CSV_PATH = BASE_DIR / "dry_run_pipeline_by_rule.csv"
STAGES_CSV_PATH = BASE_DIR / "dry_run_pipeline_stages.csv"

MIN_EDGE_FALLBACK = 15.0
MIN_EDGE = MIN_EDGE_FALLBACK
try:
    from config import MIN_EDGE as CONFIG_MIN_EDGE

    MIN_EDGE = float(CONFIG_MIN_EDGE)
except Exception:
    MIN_EDGE = MIN_EDGE_FALLBACK

DRY_RUN_FILES: dict[str, str] = {
    "relaxed_edge": "relaxed_edge_dry_run.csv",
    "long_rebound": "long_rebound_opportunity_dry_run.csv",
    "ada_opportunity": "ada_opportunity_dry_run.csv",
    "doge_link_opportunity": "doge_link_opportunity_dry_run.csv",
}

RULE_LABELS: dict[str, str] = {
    "relaxed_edge": "Relaxed Edge",
    "long_rebound": "Long Rebound",
    "ada_opportunity": "ADA Opportunity",
    "doge_link_opportunity": "DOGE/LINK Opportunity",
}

Row = dict[str, Any]
Predicate = Callable[[Row], bool]


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a value to float without raising."""
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def percent(part: int | float, total: int | float) -> float:
    """Return a rounded percentage."""
    if not total:
        return 0.0
    return round((float(part) / float(total)) * 100.0, 2)


def average(values: Iterable[float]) -> float:
    """Return a rounded average for a numeric iterable."""
    items = [float(value) for value in values]
    if not items:
        return 0.0
    return round(mean(items), 4)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read CSV rows and tolerate missing files."""
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
    """Write CSV rows with a stable header."""
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def contains_any(text: Any, fragments: Iterable[str]) -> bool:
    """Return True if text contains any fragment case-insensitively."""
    lowered = str(text or "").lower()
    return any(fragment.lower() in lowered for fragment in fragments)


def normalize_row(row: dict[str, str]) -> Row:
    """Normalize decision_debug/signals rows into one profiling shape."""
    long_score = safe_float(row.get("long_total"))
    short_score = safe_float(row.get("short_total"))
    weighted_score = max(long_score, short_score)
    diff = safe_float(row.get("diff"), abs(long_score - short_score))
    if not str(row.get("diff", "")).strip():
        diff = abs(long_score - short_score)

    direction = str(row.get("direction", "")).strip().upper()
    winner = str(row.get("winner", "")).strip().upper()
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

    signal = str(row.get("signal", row.get("decision", ""))).strip().upper()
    score = safe_float(row.get("score"))
    confidence = safe_float(row.get("confidence"))
    summary = str(row.get("summary", ""))
    risk_reason = str(row.get("risk_reason", ""))

    return {
        "timestamp": row.get("timestamp", ""),
        "symbol": str(row.get("symbol", "")).strip().upper(),
        "direction": direction,
        "candidate_direction": candidate_direction,
        "signal": signal,
        "decision": signal,
        "score": score,
        "confidence": confidence,
        "long_score": long_score,
        "short_score": short_score,
        "weighted_score": weighted_score,
        "diff": diff,
        "edge": diff,
        "edge_gap": round(MIN_EDGE - diff, 4),
        "winner": winner,
        "summary": summary,
        "risk_reason": risk_reason,
        "trend_reason": row.get("trend_reason", ""),
        "structure_reason": row.get("structure_reason", ""),
        "momentum_reason": row.get("momentum_reason", ""),
    }


def display_status(row: Row) -> str:
    """Return display-only SETUP/WATCH/NEAR SETUP/NO TRADE status."""
    signal = str(row.get("signal", "")).upper()
    score = safe_float(row.get("score"))
    confidence = safe_float(row.get("confidence"))
    weighted_score = safe_float(row.get("weighted_score"))
    edge = safe_float(row.get("edge"))
    if signal in {"SETUP", "HIGH PRIORITY"}:
        return "SETUP"
    if signal == "WATCH":
        return "WATCH"
    if (
        signal == "NO TRADE"
        and score == 0
        and confidence >= 60
        and weighted_score >= 18
        and 0 <= (MIN_EDGE - edge) <= 8
    ):
        return "NEAR SETUP"
    return "NO TRADE"


def no_trade(row: Row) -> bool:
    return str(row.get("signal", "")).upper() == "NO TRADE"


def score_zero(row: Row) -> bool:
    return safe_float(row.get("score")) == 0


def near_setup(row: Row) -> bool:
    return display_status(row) == "NEAR SETUP"


def no_clear_directional_edge(row: Row) -> bool:
    return contains_any(
        row.get("summary", ""),
        ["No clear directional edge", "directional edge", "edge"],
    )


class DryRunPipelineProfiler:
    """Profile dry-run filtering chains without changing live behavior."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.warnings: list[str] = []
        self.source_status = self._source_status()

    def build_report(self) -> dict[str, Any]:
        """Build and save the dry-run pipeline report."""
        rows = self._load_decision_rows()
        rule_profiles = self._profile_rules(rows)
        report = {
            "generated_at": utc_now(),
            "mode": "read-only dry-run pipeline profiling",
            "status": self._overall_status(rule_profiles, len(rows)),
            "min_edge_reference": MIN_EDGE,
            "source_status": self.source_status,
            "warnings": self.warnings,
            "total_decisions": len(rows),
            "overall_funnel": self._overall_funnel(rows),
            "dry_run_rules": rule_profiles,
            "actual_dry_run_rows": self._actual_dry_run_rows(),
            "main_bottlenecks": self._main_bottlenecks(rule_profiles),
            "recommendations": self._recommendations(rule_profiles, len(rows)),
            "restrictions": [
                "DecisionEngine не менялся.",
                "config.py не менялся.",
                "MIN_EDGE, MIN_SCORE, MIN_CONFIDENCE и веса не менялись.",
                "multi_timeframe_agent_v3.py не менялся.",
                "Live-логика и сделки не менялись.",
            ],
        }
        write_json(REPORT_PATH, report)
        self._write_rule_csv(rule_profiles)
        self._write_stage_csv(rule_profiles)
        SUMMARY_PATH.write_text(self._summary_text(report), encoding="utf-8")
        return report

    def print_report(self) -> None:
        """Print a compact Russian summary."""
        report = self.build_report()
        print(self._summary_text(report))

    def _source_status(self) -> dict[str, dict[str, Any]]:
        """Collect source file presence and row counts."""
        filenames = [
            "decision_debug.csv",
            "signals_v3.csv",
            *DRY_RUN_FILES.values(),
        ]
        status: dict[str, dict[str, Any]] = {}
        for filename in filenames:
            path = self.base_dir / filename
            exists = path.exists()
            rows = read_csv_rows(path) if exists else []
            if not exists:
                self.warnings.append(f"{filename}: файл отсутствует.")
            status[filename] = {
                "exists": exists,
                "row_count": len(rows),
                "size_bytes": path.stat().st_size if exists else 0,
                "message": self._source_message(exists, len(rows)),
            }
        return status

    @staticmethod
    def _source_message(exists: bool, row_count: int) -> str:
        if not exists:
            return "файл отсутствует"
        if row_count == 0:
            return "файл есть, но строк с данными пока нет"
        return "файл найден"

    def _load_decision_rows(self) -> list[Row]:
        """Load decision_debug first, then fallback to signals_v3."""
        debug_rows = read_csv_rows(self.base_dir / "decision_debug.csv")
        signal_rows = read_csv_rows(self.base_dir / "signals_v3.csv")
        source = debug_rows if debug_rows else signal_rows
        if not source:
            self.warnings.append("Нет decision_debug.csv/signals_v3.csv для профилирования.")
        return [normalize_row(row) for row in source]

    def _overall_funnel(self, rows: list[Row]) -> list[dict[str, Any]]:
        """Build a shared high-level dry-run funnel."""
        stages: list[tuple[str, str, Predicate]] = [
            ("total", "Всего решений", lambda row: True),
            ("no_trade", "decision == NO TRADE", no_trade),
            ("score_zero", "score == 0", score_zero),
            ("confidence_ge_60", "confidence >= 60", lambda row: row["confidence"] >= 60),
            ("confidence_ge_70", "confidence >= 70", lambda row: row["confidence"] >= 70),
            ("confidence_ge_75", "confidence >= 75", lambda row: row["confidence"] >= 75),
            ("weighted_ge_18", "weighted_score >= 18", lambda row: row["weighted_score"] >= 18),
            ("weighted_ge_20", "weighted_score >= 20", lambda row: row["weighted_score"] >= 20),
            ("weighted_ge_22", "weighted_score >= 22", lambda row: row["weighted_score"] >= 22),
            ("edge_ge_8", "edge >= 8", lambda row: row["edge"] >= 8),
            ("edge_ge_9", "edge >= 9", lambda row: row["edge"] >= 9),
            ("edge_lt_min_edge", "edge < MIN_EDGE", lambda row: row["edge"] < MIN_EDGE),
            ("near_setup", "status == NEAR SETUP", near_setup),
        ]
        return self._sequential_profile("overall", rows, stages)

    def _profile_rules(self, rows: list[Row]) -> dict[str, dict[str, Any]]:
        """Profile each dry-run rule with its exact filtering chain."""
        rules = self._rule_definitions()
        profiles: dict[str, dict[str, Any]] = {}
        for rule_key, stages in rules.items():
            stage_profile = self._sequential_profile(rule_key, rows, stages)
            final_rows = self._apply_stages(rows, stages)
            actual_rows = read_csv_rows(self.base_dir / DRY_RUN_FILES[rule_key])
            profiles[rule_key] = {
                "label": RULE_LABELS[rule_key],
                "historical_matches": len(final_rows),
                "actual_logged_rows": len(actual_rows),
                "capture_gap": max(0, len(final_rows) - len(actual_rows)),
                "top_symbols": dict(Counter(row["symbol"] for row in final_rows).most_common(10)),
                "avg_confidence": average(row["confidence"] for row in final_rows),
                "avg_weighted_score": average(row["weighted_score"] for row in final_rows),
                "avg_edge": average(row["edge"] for row in final_rows),
                "stage_profile": stage_profile,
                "main_bottleneck": self._rule_bottleneck(stage_profile),
            }
        return profiles

    @staticmethod
    def _rule_definitions() -> dict[str, list[tuple[str, str, Predicate]]]:
        """Return exact stage definitions for all dry-run rules."""
        return {
            "relaxed_edge": [
                ("total", "Всего решений", lambda row: True),
                ("no_trade", "decision == NO TRADE", no_trade),
                ("score_zero", "score == 0", score_zero),
                ("near_setup", "status == NEAR SETUP", near_setup),
                ("confidence_ge_75", "confidence >= 75", lambda row: row["confidence"] >= 75),
                ("weighted_ge_22", "weighted_score >= 22", lambda row: row["weighted_score"] >= 22),
                ("edge_ge_9", "edge >= 9", lambda row: row["edge"] >= 9),
                ("edge_lt_min_edge", "edge < MIN_EDGE", lambda row: row["edge"] < MIN_EDGE),
            ],
            "long_rebound": [
                ("total", "Всего решений", lambda row: True),
                ("no_trade", "decision == NO TRADE", no_trade),
                ("score_zero", "score == 0", score_zero),
                ("direction_neutral", "direction == NEUTRAL", lambda row: row["direction"] == "NEUTRAL"),
                ("confidence_ge_60", "confidence >= 60", lambda row: row["confidence"] >= 60),
                ("weighted_ge_20", "weighted_score >= 20", lambda row: row["weighted_score"] >= 20),
                ("diff_8_14", "diff between 8 and 14", lambda row: 8 <= row["edge"] <= 14),
                (
                    "risk_low_volatility",
                    'risk_text содержит "Низкая волатильность"',
                    lambda row: "Низкая волатильность" in str(row.get("risk_reason", "")),
                ),
                (
                    "risk_good_long_zone",
                    'risk_text содержит "Хорошая зона для LONG"',
                    lambda row: "Хорошая зона для LONG" in str(row.get("risk_reason", "")),
                ),
                ("no_clear_edge", "reason содержит No clear directional edge", no_clear_directional_edge),
            ],
            "ada_opportunity": [
                ("total", "Всего решений", lambda row: True),
                ("symbol_ada", "symbol == ADA/USDT", lambda row: row["symbol"] == "ADA/USDT"),
                ("no_trade", "decision == NO TRADE", no_trade),
                ("score_zero", "score == 0", score_zero),
                ("confidence_ge_60", "confidence >= 60", lambda row: row["confidence"] >= 60),
                ("weighted_ge_18", "weighted_score >= 18", lambda row: row["weighted_score"] >= 18),
                (
                    "edge_close",
                    "0 <= MIN_EDGE - diff <= 6",
                    lambda row: 0 <= (MIN_EDGE - row["edge"]) <= 6,
                ),
            ],
            "doge_link_opportunity": [
                (
                    "total",
                    "Всего решений",
                    lambda row: True,
                ),
                (
                    "symbol_doge_link",
                    "symbol in DOGE/USDT, LINK/USDT",
                    lambda row: row["symbol"] in {"DOGE/USDT", "LINK/USDT"},
                ),
                ("no_trade", "decision == NO TRADE", no_trade),
                ("score_zero", "score == 0", score_zero),
                ("weighted_ge_20", "weighted_score >= 20", lambda row: row["weighted_score"] >= 20),
                ("confidence_ge_70", "confidence >= 70", lambda row: row["confidence"] >= 70),
                ("diff_8_14", "diff between 8 and 14", lambda row: 8 <= row["edge"] <= 14),
                ("edge_reason", "reason contains directional edge", no_clear_directional_edge),
            ],
        }

    def _sequential_profile(
        self,
        rule_key: str,
        rows: list[Row],
        stages: list[tuple[str, str, Predicate]],
    ) -> list[dict[str, Any]]:
        """Apply stages sequentially and record drops."""
        total = len(rows)
        current = rows
        previous_count = len(current)
        profile: list[dict[str, Any]] = []

        for index, (stage_key, label, predicate) in enumerate(stages):
            before_rows = current
            if index == 0:
                kept_rows = [row for row in current if predicate(row)]
            else:
                kept_rows = [row for row in current if predicate(row)]
            kept_ids = {id(row) for row in kept_rows}
            lost_rows = [row for row in before_rows if id(row) not in kept_ids]
            kept_count = len(kept_rows)
            drop_count = previous_count - kept_count
            profile.append({
                "rule": rule_key,
                "stage": stage_key,
                "label": label,
                "count": kept_count,
                "percent_of_total": percent(kept_count, total),
                "drop_count": drop_count,
                "drop_percent_from_previous": percent(drop_count, previous_count),
                "top_symbols_lost": dict(
                    Counter(row["symbol"] for row in lost_rows).most_common(5)
                ),
                "avg_confidence": average(row["confidence"] for row in kept_rows),
                "avg_weighted_score": average(row["weighted_score"] for row in kept_rows),
                "avg_edge": average(row["edge"] for row in kept_rows),
            })
            current = kept_rows
            previous_count = kept_count
        return profile

    @staticmethod
    def _apply_stages(
        rows: list[Row],
        stages: list[tuple[str, str, Predicate]],
    ) -> list[Row]:
        current = rows
        for _, _, predicate in stages:
            current = [row for row in current if predicate(row)]
        return current

    @staticmethod
    def _rule_bottleneck(stage_profile: list[dict[str, Any]]) -> dict[str, Any]:
        """Find the stage with the largest absolute drop after total."""
        candidates = [
            stage
            for stage in stage_profile
            if stage.get("stage") != "total"
        ]
        if not candidates:
            return {}
        return max(
            candidates,
            key=lambda stage: (
                int(stage.get("drop_count", 0)),
                float(stage.get("drop_percent_from_previous", 0.0)),
            ),
        )

    def _actual_dry_run_rows(self) -> dict[str, int]:
        """Return current row counts in dry-run CSV files."""
        return {
            rule_key: len(read_csv_rows(self.base_dir / filename))
            for rule_key, filename in DRY_RUN_FILES.items()
        }

    def _main_bottlenecks(
        self,
        rule_profiles: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Build a ranked list of main rule bottlenecks."""
        bottlenecks = []
        for rule_key, profile in rule_profiles.items():
            bottleneck = dict(profile.get("main_bottleneck", {}))
            if not bottleneck:
                continue
            bottleneck["rule"] = rule_key
            bottleneck["rule_label"] = profile.get("label")
            bottlenecks.append(bottleneck)
        return sorted(
            bottlenecks,
            key=lambda item: (
                int(item.get("drop_count", 0)),
                float(item.get("drop_percent_from_previous", 0.0)),
            ),
            reverse=True,
        )

    @staticmethod
    def _overall_status(
        rule_profiles: dict[str, dict[str, Any]],
        total_rows: int,
    ) -> str:
        if total_rows == 0:
            return "NO_DATA"
        historical_matches = sum(
            int(profile.get("historical_matches", 0))
            for profile in rule_profiles.values()
        )
        actual_logged = sum(
            int(profile.get("actual_logged_rows", 0))
            for profile in rule_profiles.values()
        )
        if historical_matches == 0:
            return "DRY_RUN_RULES_TOO_STRICT"
        if actual_logged == 0:
            return "WAITING_FOR_LIVE_CANDIDATES"
        return "OK"

    def _recommendations(
        self,
        rule_profiles: dict[str, dict[str, Any]],
        total_rows: int,
    ) -> list[str]:
        """Build conservative Russian recommendations."""
        if total_rows == 0:
            return ["Нет данных для анализа dry-run цепочки."]

        recommendations = [
            "Live-стратегию не менять: это аудит исследовательских фильтров.",
        ]
        for rule_key, profile in rule_profiles.items():
            bottleneck = profile.get("main_bottleneck", {})
            final_count = int(profile.get("historical_matches", 0))
            if final_count == 0:
                recommendations.append(
                    f"{profile['label']}: исторических кандидатов нет; "
                    f"главный отсев на этапе '{bottleneck.get('label', 'N/A')}'."
                )
            elif int(profile.get("actual_logged_rows", 0)) == 0:
                recommendations.append(
                    f"{profile['label']}: исторически найдено {final_count}, "
                    "но live CSV пока пустой; вероятно, нужен период наблюдения."
                )
            else:
                recommendations.append(
                    f"{profile['label']}: dry-run цепочка работает, "
                    f"записано {profile.get('actual_logged_rows', 0)} строк."
                )
        return recommendations

    def _write_rule_csv(self, rule_profiles: dict[str, dict[str, Any]]) -> None:
        fields = [
            "rule",
            "label",
            "historical_matches",
            "actual_logged_rows",
            "capture_gap",
            "main_bottleneck",
            "main_bottleneck_drop",
            "avg_confidence",
            "avg_weighted_score",
            "avg_edge",
            "top_symbols",
        ]
        rows = []
        for rule_key, profile in rule_profiles.items():
            bottleneck = profile.get("main_bottleneck", {})
            rows.append({
                "rule": rule_key,
                "label": profile.get("label"),
                "historical_matches": profile.get("historical_matches"),
                "actual_logged_rows": profile.get("actual_logged_rows"),
                "capture_gap": profile.get("capture_gap"),
                "main_bottleneck": bottleneck.get("label"),
                "main_bottleneck_drop": bottleneck.get("drop_count"),
                "avg_confidence": profile.get("avg_confidence"),
                "avg_weighted_score": profile.get("avg_weighted_score"),
                "avg_edge": profile.get("avg_edge"),
                "top_symbols": json.dumps(profile.get("top_symbols", {}), ensure_ascii=False),
            })
        write_csv(RULES_CSV_PATH, rows, fields)

    def _write_stage_csv(self, rule_profiles: dict[str, dict[str, Any]]) -> None:
        fields = [
            "rule",
            "stage",
            "label",
            "count",
            "percent_of_total",
            "drop_count",
            "drop_percent_from_previous",
            "avg_confidence",
            "avg_weighted_score",
            "avg_edge",
            "top_symbols_lost",
        ]
        rows = []
        for rule_key, profile in rule_profiles.items():
            for stage in profile.get("stage_profile", []):
                rows.append({
                    **stage,
                    "top_symbols_lost": json.dumps(
                        stage.get("top_symbols_lost", {}),
                        ensure_ascii=False,
                    ),
                })
        write_csv(STAGES_CSV_PATH, rows, fields)

    def _summary_text(self, report: dict[str, Any]) -> str:
        """Format a compact Russian summary."""
        lines = [
            "====================================",
            "Dry-Run Pipeline Profiler v1",
            "====================================",
            f"Статус: {report.get('status')}",
            f"Всего решений: {report.get('total_decisions', 0)}",
            f"MIN_EDGE reference: {report.get('min_edge_reference')}",
            "",
            "Правила dry-run:",
        ]
        for rule_key, profile in report.get("dry_run_rules", {}).items():
            bottleneck = profile.get("main_bottleneck", {})
            lines.extend([
                f"- {profile.get('label')}:",
                f"  Исторических совпадений: {profile.get('historical_matches')}",
                f"  Записано в CSV: {profile.get('actual_logged_rows')}",
                f"  Главный отсев: {bottleneck.get('label', 'N/A')}",
                f"  Потеря на этапе: {bottleneck.get('drop_count', 0)}",
            ])
        lines.extend([
            "",
            "Главные bottleneck:",
        ])
        for item in report.get("main_bottlenecks", [])[:5]:
            lines.append(
                "- "
                f"{item.get('rule_label')}: {item.get('label')} "
                f"(-{item.get('drop_count')}, "
                f"{item.get('drop_percent_from_previous')}%)"
            )
        lines.extend([
            "",
            "Рекомендации:",
        ])
        for recommendation in report.get("recommendations", []):
            lines.append(f"- {recommendation}")
        lines.extend([
            "",
            "Файлы:",
            f"- {REPORT_PATH.name}",
            f"- {SUMMARY_PATH.name}",
            f"- {RULES_CSV_PATH.name}",
            f"- {STAGES_CSV_PATH.name}",
        ])
        return "\n".join(lines)


def main() -> None:
    """Run the profiler from the command line."""
    profiler = DryRunPipelineProfiler()
    profiler.print_report()


if __name__ == "__main__":
    main()
