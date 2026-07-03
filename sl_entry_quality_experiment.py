"""SL / Entry Quality Calibration experiment.

Read-only experiment that checks whether blocking or downgrading signals with
weak SL quality would have improved closed-trade results. It uses existing
analytics artifacts and never changes DecisionEngine, config.py, strategy
weights, live agent behavior, or trading logic.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence


BASE_DIR = Path(__file__).resolve().parent
PATTERN_REPORT = BASE_DIR / "trade_pattern_discovery_report.json"
COMPARATOR_REPORT = BASE_DIR / "trade_comparator_report.json"

REPORT_JSON = BASE_DIR / "sl_entry_quality_experiment_report.json"
SUMMARY_TEXT = BASE_DIR / "sl_entry_quality_experiment_summary.txt"
RESULTS_CSV = BASE_DIR / "sl_entry_quality_experiment_results.csv"
TRADES_CSV = BASE_DIR / "sl_entry_quality_experiment_trades.csv"

SCENARIOS: Sequence[str] = (
    "baseline",
    "block_sl_quality_D",
    "block_sl_quality_D_E",
    "downgrade_sl_quality_D",
    "block_short_sl_quality_D",
)
ENTRY_SIGNALS = {"SETUP", "HIGH PRIORITY"}
DOWNGRADE_MAP = {
    "HIGH PRIORITY": "SETUP",
    "SETUP": "WATCH",
    "WATCH": "WAIT",
    "WAIT": "NO TRADE",
    "NO TRADE": "NO TRADE",
    "": "",
}


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a value to float."""
    try:
        text = str(value).replace("%", "").strip()
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def read_json(path: Path) -> Dict[str, Any]:
    """Read JSON dict from disk."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON report."""
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)


def profit_factor(pnls: Sequence[float]) -> float:
    """Calculate profit factor."""
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = abs(sum(value for value in pnls if value < 0))
    if gross_loss == 0:
        return round(gross_profit, 4) if gross_profit else 0.0
    return round(gross_profit / gross_loss, 4)


def metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Calculate requested metrics for a trade subset."""
    wins = [row for row in rows if row.get("result") == "WIN"]
    losses = [row for row in rows if row.get("result") == "LOSS"]
    pnls = [safe_float(row.get("pnl")) for row in rows]
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round(len(wins) / len(rows) * 100, 2) if rows else 0.0,
        "profit_factor": profit_factor(pnls),
        "net_pnl": round(sum(pnls), 8),
    }


def scenario_signal(row: Mapping[str, Any], scenario: str) -> str:
    """Return scenario-adjusted signal."""
    signal = str(row.get("decision", ""))
    sl_quality = str(row.get("sl_quality", "")).upper()
    direction = str(row.get("direction", "")).upper()
    if scenario == "baseline":
        return signal
    if scenario == "block_sl_quality_D" and sl_quality == "D":
        return "NO TRADE"
    if scenario == "block_sl_quality_D_E" and sl_quality in {"D", "E"}:
        return "NO TRADE"
    if scenario == "downgrade_sl_quality_D" and sl_quality == "D":
        return DOWNGRADE_MAP.get(signal, signal)
    if scenario == "block_short_sl_quality_D" and direction == "SHORT" and sl_quality == "D":
        return "NO TRADE"
    return signal


def prevented(row: Mapping[str, Any], scenario: str) -> bool:
    """Return True if scenario would prevent the original entry."""
    baseline_signal = str(row.get("decision", ""))
    adjusted = scenario_signal(row, scenario)
    return baseline_signal in ENTRY_SIGNALS and adjusted not in ENTRY_SIGNALS


class SLEntryQualityExperiment:
    """Run read-only SL/entry quality calibration scenarios."""

    def __init__(self) -> None:
        self.pattern_report = read_json(PATTERN_REPORT)
        self.comparator_report = read_json(COMPARATOR_REPORT)

    def load_profiles(self) -> List[Dict[str, Any]]:
        """Load profile rows from pattern discovery or comparator fallback."""
        rows = self.pattern_report.get("profiles", [])
        if isinstance(rows, list) and rows:
            return [dict(row) for row in rows if isinstance(row, Mapping)]
        rows = self.comparator_report.get("trades", [])
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, Mapping)]
        return []

    def build_report(self) -> Dict[str, Any]:
        """Build experiment report and artifacts."""
        profiles = self.load_profiles()
        scenario_results = {
            scenario: self.run_scenario(profiles, scenario)
            for scenario in SCENARIOS
        }
        report = {
            "status": "OK" if profiles else "NO_TRADES",
            "source": "trade_pattern_discovery_report.json",
            "rules": {
                "block_sl_quality_D": "Block any entry with sl_quality=D",
                "block_sl_quality_D_E": "Block any entry with sl_quality=D/E",
                "downgrade_sl_quality_D": "Downgrade HIGH PRIORITY->SETUP and SETUP->WATCH when sl_quality=D",
                "block_short_sl_quality_D": "Block SHORT entries with sl_quality=D",
            },
            "baseline": scenario_results.get("baseline", {}),
            "scenarios": scenario_results,
            "best_candidate": self.best_candidate(scenario_results),
            "recommendation": self.recommendation(scenario_results),
            "trade_rows": self.trade_rows(profiles),
        }
        write_json(REPORT_JSON, report)
        self.write_results_csv(scenario_results)
        self.write_trades_csv(profiles)
        self.write_summary(report)
        return report

    def run_scenario(
        self,
        profiles: Sequence[Mapping[str, Any]],
        scenario: str,
    ) -> Dict[str, Any]:
        """Run one scenario against closed trade profiles."""
        prevented_rows = [row for row in profiles if prevented(row, scenario)]
        remaining = [row for row in profiles if row not in prevented_rows]
        prevented_losses = [row for row in prevented_rows if row.get("result") == "LOSS"]
        lost_wins = [row for row in prevented_rows if row.get("result") == "WIN"]
        payload = metrics(remaining)
        baseline_net = sum(safe_float(row.get("pnl")) for row in profiles)
        payload.update(
            {
                "scenario": scenario,
                "prevented_trades": len(prevented_rows),
                "prevented_losses": len(prevented_losses),
                "lost_wins": len(lost_wins),
                "prevented_loss_pnl": round(sum(safe_float(row.get("pnl")) for row in prevented_losses), 8),
                "lost_win_pnl": round(sum(safe_float(row.get("pnl")) for row in lost_wins), 8),
                "net_pnl_delta": round(payload["net_pnl"] - baseline_net, 8),
            }
        )
        return payload

    @staticmethod
    def best_candidate(results: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
        """Pick best non-baseline scenario by net delta and loss prevention."""
        candidates = [
            payload for name, payload in results.items()
            if name != "baseline"
        ]
        if not candidates:
            return {}
        best = max(
            candidates,
            key=lambda item: (
                safe_float(item.get("net_pnl_delta")),
                safe_float(item.get("prevented_losses")) - safe_float(item.get("lost_wins")),
                safe_float(item.get("profit_factor")),
            ),
        )
        return dict(best)

    @staticmethod
    def recommendation(results: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
        """Create cautious read-only recommendation."""
        best = SLEntryQualityExperiment.best_candidate(results)
        target = results.get("block_short_sl_quality_D", {})
        if not best:
            return {
                "status": "NO_DATA",
                "reason": "No closed trade profiles available.",
                "apply_automatically": False,
            }
        if (
            safe_float(target.get("prevented_losses")) > safe_float(target.get("lost_wins"))
            and safe_float(target.get("lost_wins")) <= 1
            and safe_float(target.get("net_pnl_delta")) > 0
        ):
            return {
                "status": "CANDIDATE_FOR_DRY_RUN",
                "best_scenario": "block_short_sl_quality_D",
                "reason": (
                    "block_short_sl_quality_D prevented more LOSS than WIN and improved historical net PnL."
                ),
                "apply_automatically": False,
            }
        if safe_float(best.get("net_pnl_delta")) > 0:
            return {
                "status": "CANDIDATE_FOR_REPLAY",
                "best_scenario": best.get("scenario"),
                "reason": "Best SL quality scenario improved historical metrics, but needs replay/dry-run.",
                "apply_automatically": False,
            }
        return {
            "status": "NOT_RECOMMENDED",
            "best_scenario": best.get("scenario"),
            "reason": "No scenario improved historical net PnL safely.",
            "apply_automatically": False,
        }

    @staticmethod
    def trade_rows(profiles: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Return per-trade scenario flags."""
        rows = []
        for row in profiles:
            output = {
                "symbol": row.get("symbol"),
                "direction": row.get("direction"),
                "result": row.get("result"),
                "pnl": row.get("pnl"),
                "decision": row.get("decision"),
                "score": row.get("score"),
                "confidence": row.get("confidence"),
                "trend": row.get("trend"),
                "local_trend": row.get("local_trend"),
                "entry_quality": row.get("entry_quality"),
                "sl_quality": row.get("sl_quality"),
            }
            for scenario in SCENARIOS:
                output[f"{scenario}_signal"] = scenario_signal(row, scenario)
                output[f"{scenario}_prevented"] = prevented(row, scenario)
            rows.append(output)
        return rows

    @staticmethod
    def write_results_csv(results: Mapping[str, Mapping[str, Any]]) -> None:
        """Write scenario metrics."""
        fieldnames = [
            "scenario",
            "trades",
            "wins",
            "losses",
            "winrate",
            "profit_factor",
            "net_pnl",
            "prevented_losses",
            "lost_wins",
            "prevented_trades",
            "net_pnl_delta",
        ]
        with RESULTS_CSV.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for scenario in SCENARIOS:
                row = dict(results.get(scenario, {}))
                row["scenario"] = scenario
                writer.writerow({field: row.get(field, "") for field in fieldnames})

    @staticmethod
    def write_trades_csv(profiles: Sequence[Mapping[str, Any]]) -> None:
        """Write per-trade scenario flags."""
        rows = SLEntryQualityExperiment.trade_rows(profiles)
        fieldnames = [
            "symbol",
            "direction",
            "result",
            "pnl",
            "decision",
            "score",
            "confidence",
            "trend",
            "local_trend",
            "entry_quality",
            "sl_quality",
        ]
        for scenario in SCENARIOS:
            fieldnames.extend([f"{scenario}_signal", f"{scenario}_prevented"])
        with TRADES_CSV.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in fieldnames})

    @staticmethod
    def write_summary(report: Mapping[str, Any]) -> None:
        """Write human-readable summary."""
        lines = [
            "SL / Entry Quality Experiment",
            "=============================",
            f"Status: {report.get('status')}",
            "",
            "Scenarios:",
        ]
        for scenario in SCENARIOS:
            payload = report.get("scenarios", {}).get(scenario, {})
            lines.append(
                f"- {scenario}: trades={payload.get('trades')} "
                f"wins={payload.get('wins')} losses={payload.get('losses')} "
                f"WR={payload.get('winrate')}% PF={payload.get('profit_factor')} "
                f"net={payload.get('net_pnl')} prevented_losses={payload.get('prevented_losses')} "
                f"lost_wins={payload.get('lost_wins')} delta={payload.get('net_pnl_delta')}"
            )
        best = report.get("best_candidate", {})
        rec = report.get("recommendation", {})
        lines.extend(
            [
                "",
                "Best candidate:",
                f"- {best.get('scenario')}",
                f"- Net delta: {best.get('net_pnl_delta')}",
                f"- Prevented losses: {best.get('prevented_losses')}",
                f"- Lost wins: {best.get('lost_wins')}",
                "",
                "Recommendation:",
                f"- Status: {rec.get('status')}",
                f"- Best scenario: {rec.get('best_scenario')}",
                f"- Reason: {rec.get('reason')}",
                "- Apply automatically: false",
                "",
                "Important:",
                "- Read-only experiment.",
                "- DecisionEngine, config, weights, and live trading logic were not changed.",
            ]
        )
        SUMMARY_TEXT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def print_report(report: Mapping[str, Any]) -> None:
        """Print compact terminal report."""
        baseline = report.get("baseline", {})
        best = report.get("best_candidate", {})
        rec = report.get("recommendation", {})
        print("SL / Entry Quality Experiment")
        print(f"Baseline: trades={baseline.get('trades')} WR={baseline.get('winrate')} PF={baseline.get('profit_factor')} net={baseline.get('net_pnl')}")
        print(f"Best: {best.get('scenario')} delta={best.get('net_pnl_delta')} prevented_losses={best.get('prevented_losses')} lost_wins={best.get('lost_wins')}")
        print(f"Recommendation: {rec.get('status')} ({rec.get('best_scenario')})")
        print(f"JSON: {REPORT_JSON.name}")
        print(f"Summary: {SUMMARY_TEXT.name}")
        print(f"CSV: {RESULTS_CSV.name}")


def main() -> None:
    """CLI entry point."""
    experiment = SLEntryQualityExperiment()
    report = experiment.build_report()
    experiment.print_report(report)


if __name__ == "__main__":
    main()
