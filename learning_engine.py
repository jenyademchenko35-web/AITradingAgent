"""Learning Engine v1 for AITradingAgent.

This module analyzes accumulated CSV/JSON artifacts and produces a structured
learning report. It does not change trading logic, weights, filters, or any
runtime configuration.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional


BASE_DIR = Path(__file__).resolve().parent

DECISION_DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DECISION_EXPLANATIONS_FILE = BASE_DIR / "decision_explanations.csv"
DECISION_DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
OPTIMIZER_RESULTS_FILE = BASE_DIR / "optimizer_results.csv"

OUTPUT_FILE = BASE_DIR / "learning_report.json"

SIGNAL_ORDER = ("HIGH PRIORITY", "SETUP", "WATCH", "WAIT", "NO TRADE")
FILTER_ORDER = ("Trend", "Structure", "Momentum", "Risk")


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert values from CSV/JSON to float."""
    try:
        text = str(value).replace("%", "").strip()
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read CSV rows while tolerating empty files and repeated headers."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    clean_rows = []
    for row in rows:
        if not row or not any(row.values()):
            continue
        if row.get("timestamp") == "timestamp":
            continue
        clean_rows.append(row)
    return clean_rows


class LearningEngine:
    """Build an independent strategy-learning report from accumulated data."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or BASE_DIR
        self.warnings: List[str] = []

    def build_report(self) -> Dict[str, Any]:
        """Build the full learning report and save it to disk."""
        self.warnings = []
        signals_rows = self._load_rows(SIGNALS_FILE, "signals_v3.csv")
        diagnostics_rows = self._load_rows(
            DECISION_DIAGNOSTICS_FILE,
            "decision_diagnostics.csv",
        )
        debug_rows = self._load_rows(DECISION_DEBUG_FILE, "decision_debug.csv")
        explanations_rows = self._load_rows(
            DECISION_EXPLANATIONS_FILE,
            "decision_explanations.csv",
        )
        optimizer_rows = self._load_rows(
            OPTIMIZER_RESULTS_FILE,
            "optimizer_results.csv",
        )

        signals = self._build_signals_section(signals_rows)
        filters = self._build_filters_section(diagnostics_rows)
        optimizer = self._build_optimizer_section(optimizer_rows)
        symbols = self._build_symbols_section(signals_rows)
        recommendations = self._build_recommendations(
            signals=signals,
            filters=filters,
            optimizer=optimizer,
            symbols=symbols,
            debug_rows=debug_rows,
            explanations_rows=explanations_rows,
        )

        report = {
            "generated_at": utc_now(),
            "warnings": self.warnings,
            "signals": signals,
            "filters": filters,
            "optimizer": optimizer,
            "symbols": symbols,
            "recommendations": recommendations,
        }
        self._save_report(report)
        return report

    def print_report(self) -> None:
        """Print the learning report in a readable terminal format."""
        report = self.build_report()

        print("Learning Engine v1")
        print(f"Generated at : {report.get('generated_at', '')}")
        if report["warnings"]:
            print("Warnings     :")
            for item in report["warnings"]:
                print(f"- {item}")
        print()

        signals = report["signals"]
        print("Signals")
        print(f"Total decisions: {signals.get('total_decisions', 0)}")
        for name in SIGNAL_ORDER:
            payload = signals.get("distribution", {}).get(name, {})
            print(
                f"{name:<13}: {payload.get('count', 0)} "
                f"({payload.get('percent', 0)}%)"
            )
        print()

        filters = report["filters"]
        print("Filters")
        print(f"Primary blocker: {filters.get('primary_blocker', 'N/A')}")
        print(f"Average lost score     : {filters.get('average_lost_score', 0)}")
        print(f"Average potential score: {filters.get('average_potential_score', 0)}")
        for name in FILTER_ORDER:
            payload = filters.get("blockers", {}).get(name, {})
            print(
                f"{name:<10}: {payload.get('count', 0)} "
                f"({payload.get('percent', 0)}%)"
            )
        print()

        optimizer = report["optimizer"]
        print("Optimizer")
        print(f"Runs               : {optimizer.get('optimization_runs', 0)}")
        print(f"Best ATR           : {optimizer.get('best_atr', 'N/A')}")
        print(f"Best RR            : {optimizer.get('best_rr', 'N/A')}")
        print(f"Max Profit Factor  : {optimizer.get('max_profit_factor', 0)}")
        print(f"Average ProfitFactor: {optimizer.get('average_profit_factor', 0)}")
        print(f"Average WinRate    : {optimizer.get('average_winrate', 0)}")
        print()

        print("Symbols")
        for symbol, payload in report["symbols"].items():
            print(
                f"{symbol}: decisions={payload.get('decisions', 0)} | "
                f"avg_score={payload.get('average_score', 0)} | "
                f"avg_confidence={payload.get('average_confidence', 0)} | "
                f"setup={payload.get('setup_count', 0)} | "
                f"high_priority={payload.get('high_priority_count', 0)}"
            )
        print()

        print("Recommendations")
        for item in report["recommendations"]:
            print(f"- {item}")

    def _load_rows(self, path: Path, label: str) -> List[Dict[str, str]]:
        """Load rows and record a warning if a file is missing or empty."""
        rows = read_csv_rows(path)
        if not path.exists():
            self.warnings.append(f"{label} is missing.")
        elif path.stat().st_size == 0:
            self.warnings.append(f"{label} is empty.")
        elif not rows:
            self.warnings.append(f"{label} has no usable rows.")
        return rows

    def _build_signals_section(
        self,
        signals_rows: List[Mapping[str, str]],
    ) -> Dict[str, Any]:
        """Build signal counts and distribution."""
        total = len(signals_rows)
        counts = Counter(row.get("signal", "UNKNOWN") for row in signals_rows)
        distribution = {}
        for signal in SIGNAL_ORDER:
            count = counts.get(signal, 0)
            distribution[signal] = {
                "count": count,
                "percent": round((count / total * 100) if total else 0.0, 2),
            }
        return {
            "total_decisions": total,
            "distribution": distribution,
        }

    def _build_filters_section(
        self,
        diagnostics_rows: List[Mapping[str, str]],
    ) -> Dict[str, Any]:
        """Build blocker and score statistics from diagnostics."""
        total = len(diagnostics_rows)
        blockers = Counter(row.get("primary_blocker", "") for row in diagnostics_rows)
        blocker_distribution = {}
        for name in FILTER_ORDER:
            count = blockers.get(name, 0)
            blocker_distribution[name] = {
                "count": count,
                "percent": round((count / total * 100) if total else 0.0, 2),
            }
        avg_lost = round(
            sum(safe_float(row.get("lost_score")) for row in diagnostics_rows) / total,
            2,
        ) if total else 0.0
        avg_potential = round(
            sum(safe_float(row.get("potential_score")) for row in diagnostics_rows) / total,
            2,
        ) if total else 0.0
        return {
            "rows": total,
            "primary_blocker": self._top_key(blockers),
            "blockers": blocker_distribution,
            "average_lost_score": avg_lost,
            "average_potential_score": avg_potential,
        }

    def _build_optimizer_section(
        self,
        optimizer_rows: List[Mapping[str, str]],
    ) -> Dict[str, Any]:
        """Build optimizer summary from optimizer_results.csv."""
        if not optimizer_rows:
            return {
                "optimization_runs": 0,
                "best_atr": "N/A",
                "best_rr": "N/A",
                "max_profit_factor": 0.0,
                "average_profit_factor": 0.0,
                "average_winrate": 0.0,
            }

        best_row = max(
            optimizer_rows,
            key=lambda row: (
                safe_float(row.get("ProfitFactor")),
                safe_float(row.get("WinRate")),
            ),
        )
        avg_pf = sum(safe_float(row.get("ProfitFactor")) for row in optimizer_rows)
        avg_wr = sum(safe_float(row.get("WinRate")) for row in optimizer_rows)
        return {
            "optimization_runs": len(optimizer_rows),
            "best_atr": best_row.get("ATR", "N/A"),
            "best_rr": best_row.get("RR", "N/A"),
            "max_profit_factor": round(
                max(safe_float(row.get("ProfitFactor")) for row in optimizer_rows),
                2,
            ),
            "average_profit_factor": round(avg_pf / len(optimizer_rows), 2),
            "average_winrate": round(avg_wr / len(optimizer_rows), 2),
        }

    def _build_symbols_section(
        self,
        signals_rows: List[Mapping[str, str]],
    ) -> Dict[str, Any]:
        """Build per-symbol statistics from signals."""
        by_symbol: Dict[str, List[Mapping[str, str]]] = defaultdict(list)
        for row in signals_rows:
            symbol = row.get("symbol", "")
            if symbol:
                by_symbol[symbol].append(row)

        result: Dict[str, Any] = {}
        for symbol in sorted(by_symbol):
            rows = by_symbol[symbol]
            total = len(rows)
            result[symbol] = {
                "decisions": total,
                "average_score": round(
                    sum(safe_float(row.get("score")) for row in rows) / total,
                    2,
                ) if total else 0.0,
                "average_confidence": round(
                    sum(safe_float(row.get("confidence")) for row in rows) / total,
                    2,
                ) if total else 0.0,
                "setup_count": sum(1 for row in rows if row.get("signal") == "SETUP"),
                "high_priority_count": sum(
                    1 for row in rows if row.get("signal") == "HIGH PRIORITY"
                ),
            }
        return result

    def _build_recommendations(
        self,
        signals: Mapping[str, Any],
        filters: Mapping[str, Any],
        optimizer: Mapping[str, Any],
        symbols: Mapping[str, Any],
        debug_rows: List[Mapping[str, str]],
        explanations_rows: List[Mapping[str, str]],
    ) -> List[str]:
        """Build a conservative recommendation list from the report."""
        recommendations: List[str] = []

        primary = filters.get("primary_blocker", "N/A")
        primary_payload = filters.get("blockers", {}).get(primary, {})
        if primary != "N/A":
            recommendations.append(
                f"{primary} filter blocks {primary_payload.get('percent', 0)}% of diagnostics decisions."
            )

        for name in FILTER_ORDER:
            payload = filters.get("blockers", {}).get(name, {})
            if payload.get("percent", 0) >= 30:
                recommendations.append(
                    f"{name} filter rejects too many candidates at {payload.get('percent', 0)}%."
                )

        if optimizer.get("optimization_runs", 0) > 0:
            recommendations.append(
                f"ATR {optimizer.get('best_atr', 'N/A')} remains optimal."
            )
            recommendations.append(
                f"RR {optimizer.get('best_rr', 'N/A')} remains optimal."
            )

        best_symbol_score = self._top_symbol(symbols, "average_score")
        if best_symbol_score:
            recommendations.append(
                f"{best_symbol_score} produces the highest average score."
            )

        best_symbol_setup = self._top_symbol(symbols, "setup_count")
        if best_symbol_setup:
            recommendations.append(
                f"{best_symbol_setup} has the highest setup frequency."
            )

        total_decisions = signals.get("total_decisions", 0)
        no_trade = signals.get("distribution", {}).get("NO TRADE", {}).get("percent", 0)
        if total_decisions and no_trade >= 50:
            recommendations.append(
                f"NO TRADE dominates signal flow at {no_trade}%."
            )

        if debug_rows and explanations_rows:
            gap = abs(len(debug_rows) - len(explanations_rows))
            if gap > 5:
                recommendations.append(
                    f"Debug/explanations coverage differs by {gap} rows and should be monitored."
                )

        if not recommendations:
            recommendations.append(
                "Not enough data for strong recommendations yet."
            )
        return recommendations

    def _top_key(self, counter: Counter[str]) -> str:
        """Return the key with the highest count from a Counter."""
        if not counter:
            return "N/A"
        return counter.most_common(1)[0][0]

    def _top_symbol(self, symbols: Mapping[str, Any], field: str) -> str:
        """Return the symbol with the highest value for a given field."""
        if not symbols:
            return ""
        return max(
            symbols.items(),
            key=lambda item: safe_float(item[1].get(field)),
        )[0]

    def _save_report(self, report: Mapping[str, Any]) -> None:
        """Persist the report to learning_report.json."""
        with OUTPUT_FILE.open("w", encoding="utf-8") as file:
            json.dump(report, file, indent=2, ensure_ascii=False)


def main() -> None:
    """Run Learning Engine v1 as a standalone module."""
    engine = LearningEngine()
    engine.print_report()


if __name__ == "__main__":
    main()
