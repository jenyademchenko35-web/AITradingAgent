"""Recommendation Engine v1 for AITradingAgent.

This module reads learning_report.json and produces conservative parameter
recommendations. It never changes config, weights, or trading logic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping

from config import ATR_MULT, RISK_REWARD


BASE_DIR = Path(__file__).resolve().parent
LEARNING_REPORT_FILE = BASE_DIR / "learning_report.json"
OUTPUT_FILE = BASE_DIR / "recommendations.json"


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert arbitrary values to float."""
    try:
        text = str(value).replace("%", "").strip()
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def read_json(path: Path) -> Dict[str, Any]:
    """Read a JSON object from disk."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


class RecommendationEngine:
    """Generate cautious recommendations from a learning report."""

    def __init__(self, learning_report_path: Path | None = None) -> None:
        self.learning_report_path = learning_report_path or LEARNING_REPORT_FILE
        self.warnings: List[str] = []

    def build_report(self) -> Dict[str, Any]:
        """Build recommendation output from learning_report.json."""
        self.warnings = []
        learning_report = read_json(self.learning_report_path)
        if not learning_report:
            self.warnings.append("learning_report.json is missing or empty.")
            report = {
                "generated_at": utc_now(),
                "status": "NO_DATA",
                "recommendations": [],
                "message": "No reliable recommendations yet.",
                "warnings": self.warnings,
            }
            self._save_report(report)
            return report

        self.warnings.extend(learning_report.get("warnings", []))

        signals = learning_report.get("signals", {})
        filters = learning_report.get("filters", {})
        optimizer = learning_report.get("optimizer", {})

        total_decisions = int(signals.get("total_decisions", 0))
        diagnostics_rows = int(filters.get("rows", 0))
        optimization_runs = int(optimizer.get("optimization_runs", 0))

        recommendations = self._build_parameter_recommendations(
            optimizer=optimizer,
            total_decisions=total_decisions,
            diagnostics_rows=diagnostics_rows,
        )

        status = "READY" if recommendations else "NO_RELIABLE_RECOMMENDATIONS"
        message = (
            "Recommendations generated from sufficient learning statistics."
            if recommendations
            else "No reliable recommendations yet."
        )

        report = {
            "generated_at": utc_now(),
            "status": status,
            "recommendations": recommendations,
            "message": message,
            "warnings": self.warnings,
            "evidence": {
                "total_decisions": total_decisions,
                "diagnostics_rows": diagnostics_rows,
                "optimization_runs": optimization_runs,
            },
        }
        self._save_report(report)
        return report

    def print_report(self) -> None:
        """Print the recommendation report in a compact terminal format."""
        report = self.build_report()

        print("Recommendation Engine v1")
        print(f"Generated at : {report.get('generated_at', '')}")
        print(f"Status       : {report.get('status', 'N/A')}")
        print(f"Message      : {report.get('message', 'N/A')}")
        if report.get("warnings"):
            print("Warnings     :")
            for item in report["warnings"]:
                print(f"- {item}")
        print()

        recommendations = report.get("recommendations", [])
        if not recommendations:
            print("No reliable recommendations yet.")
            return

        for item in recommendations:
            print(f"Parameter    : {item.get('parameter', 'N/A')}")
            print(f"Current      : {item.get('current', 'N/A')}")
            print(f"Recommended  : {item.get('recommended', 'N/A')}")
            print(f"Confidence   : {item.get('confidence', 'N/A')}")
            print(f"Reason       : {item.get('reason', 'N/A')}")
            print()

    def _build_parameter_recommendations(
        self,
        optimizer: Mapping[str, Any],
        total_decisions: int,
        diagnostics_rows: int,
    ) -> List[Dict[str, Any]]:
        """Build cautious parameter recommendations from optimizer evidence."""
        recommendations: List[Dict[str, Any]] = []

        optimization_runs = int(optimizer.get("optimization_runs", 0))
        best_atr = optimizer.get("best_atr", "N/A")
        best_rr = optimizer.get("best_rr", "N/A")
        max_pf = safe_float(optimizer.get("max_profit_factor"))
        avg_pf = safe_float(optimizer.get("average_profit_factor"))
        avg_wr = safe_float(optimizer.get("average_winrate"))

        enough_data = total_decisions >= 1000 and diagnostics_rows >= 200
        enough_optimizer = optimization_runs >= 5
        strong_optimizer_edge = max_pf >= avg_pf + 0.10 and avg_wr > 0

        if not enough_data:
            self.warnings.append(
                "Signal/diagnostics history is still too small for reliable parameter recommendations."
            )
            return recommendations
        if not enough_optimizer:
            self.warnings.append(
                "Optimizer history is insufficient for reliable ATR/RR recommendations."
            )
            return recommendations
        if not strong_optimizer_edge:
            self.warnings.append(
                "Optimizer results do not show a strong enough edge over average performance."
            )
            return recommendations

        if self._is_numeric(best_atr) and safe_float(best_atr) != safe_float(ATR_MULT):
            recommendations.append(
                {
                    "parameter": "ATR_MULT",
                    "current": ATR_MULT,
                    "recommended": safe_float(best_atr),
                    "confidence": self._confidence_score(
                        max_pf=max_pf,
                        avg_pf=avg_pf,
                        optimization_runs=optimization_runs,
                    ),
                    "reason": "Highest ProfitFactor over optimization history.",
                }
            )

        if self._is_numeric(best_rr) and safe_float(best_rr) != safe_float(RISK_REWARD):
            recommendations.append(
                {
                    "parameter": "RISK_REWARD",
                    "current": RISK_REWARD,
                    "recommended": safe_float(best_rr),
                    "confidence": self._confidence_score(
                        max_pf=max_pf,
                        avg_pf=avg_pf,
                        optimization_runs=optimization_runs,
                    ),
                    "reason": "Best optimizer combination outperformed average historical WinRate/ProfitFactor.",
                }
            )

        return recommendations

    def _confidence_score(
        self,
        max_pf: float,
        avg_pf: float,
        optimization_runs: int,
    ) -> float:
        """Return a conservative confidence score."""
        pf_gap = max(0.0, max_pf - avg_pf)
        run_factor = min(0.15, optimization_runs * 0.01)
        confidence = 0.65 + min(0.20, pf_gap) + run_factor
        return round(min(confidence, 0.95), 2)

    def _is_numeric(self, value: Any) -> bool:
        """Return True when a value can be safely interpreted as numeric."""
        try:
            float(str(value).strip())
        except (TypeError, ValueError):
            return False
        return True

    def _save_report(self, report: Mapping[str, Any]) -> None:
        """Persist the recommendation report to recommendations.json."""
        with OUTPUT_FILE.open("w", encoding="utf-8") as file:
            json.dump(report, file, indent=2, ensure_ascii=False)


def main() -> None:
    """Run Recommendation Engine v1 as a standalone module."""
    engine = RecommendationEngine()
    engine.print_report()


if __name__ == "__main__":
    main()
