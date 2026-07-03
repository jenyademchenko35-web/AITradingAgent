"""Trend/Momentum conflict experiment for AITradingAgent.

This read-only experiment checks a specific v2 candidate rule:

If direction is SHORT, 1h EMA trend is BULLISH, and Momentum is FAIL,
then downgrade the signal or block it completely.

The script compares the rule against baseline closed trades and never changes
DecisionEngine, config.py, strategy weights, or live trading behavior.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


BASE_DIR = Path(__file__).resolve().parent
TRADES_FILE = BASE_DIR / "trades.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"

REPORT_JSON = BASE_DIR / "trend_momentum_conflict_report.json"
SUMMARY_TEXT = BASE_DIR / "trend_momentum_conflict_summary.txt"
TRADES_CSV = BASE_DIR / "trend_momentum_conflict_trades.csv"

TRADE_SIGNALS = {"SETUP", "HIGH PRIORITY"}
DOWNGRADE_MAP = {
    "HIGH PRIORITY": "SETUP",
    "SETUP": "WATCH",
    "WATCH": "WAIT",
    "WAIT": "NO TRADE",
    "NO TRADE": "NO TRADE",
}


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: Any) -> Optional[datetime]:
    """Parse project ISO timestamps as UTC datetimes."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert values to float."""
    try:
        text = str(value).replace("%", "").strip()
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def optional_float(value: Any) -> Optional[float]:
    """Convert values to float, preserving missing values."""
    try:
        text = str(value).replace("%", "").strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read CSV rows while skipping empty rows and repeated headers."""
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


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON report."""
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)


def group_by_symbol(rows: Iterable[Mapping[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    """Group rows by symbol and sort by timestamp."""
    grouped: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        symbol = row.get("symbol", "")
        if symbol:
            grouped[symbol].append(dict(row))
    for symbol_rows in grouped.values():
        symbol_rows.sort(key=lambda item: item.get("timestamp", ""))
    return grouped


def nearest_before(
    rows: Iterable[Mapping[str, str]],
    target_time: Optional[datetime],
    direction: str = "",
) -> Optional[Dict[str, str]]:
    """Find nearest row at or before target_time, optionally matching direction."""
    if target_time is None:
        return None
    best_row: Optional[Dict[str, str]] = None
    best_time: Optional[datetime] = None
    for row in rows:
        row_time = parse_time(row.get("timestamp"))
        if row_time is None or row_time > target_time:
            continue
        row_direction = row.get("direction", "")
        if direction and row_direction not in {"", direction, "NEUTRAL"}:
            continue
        if best_time is None or row_time > best_time:
            best_time = row_time
            best_row = dict(row)
    return best_row


def contribution(debug_row: Mapping[str, str], direction: str, engine: str) -> float:
    """Return direction-specific engine contribution."""
    return safe_float(debug_row.get(f"{engine}_{direction.lower()}"))


def opposite_direction(direction: str) -> str:
    """Return opposite trade direction."""
    return "SHORT" if direction == "LONG" else "LONG"


def infer_pnl(trade: Mapping[str, str]) -> float:
    """Return PnL using stored pnl or a stop/take fallback."""
    stored = optional_float(trade.get("pnl"))
    if stored is not None:
        return stored
    direction = str(trade.get("direction", "")).upper()
    result = str(trade.get("result") or trade.get("status", "")).upper()
    entry = optional_float(trade.get("entry"))
    exit_price = optional_float(trade.get("exit_price"))
    if exit_price is None and result == "WIN":
        exit_price = optional_float(trade.get("take_profit"))
    if exit_price is None and result == "LOSS":
        exit_price = optional_float(trade.get("stop_loss"))
    if entry is None or exit_price is None:
        return 0.0
    if direction == "SHORT":
        return round(entry - exit_price, 8)
    if direction == "LONG":
        return round(exit_price - entry, 8)
    return 0.0


def profit_factor(pnls: Sequence[float]) -> float:
    """Calculate profit factor."""
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = abs(sum(value for value in pnls if value < 0))
    if gross_loss == 0:
        return round(gross_profit, 4) if gross_profit else 0.0
    return round(gross_profit / gross_loss, 4)


def metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Calculate trade metrics."""
    pnls = [safe_float(row.get("pnl")) for row in rows]
    wins = sum(1 for row in rows if row.get("result") == "WIN")
    losses = sum(1 for row in rows if row.get("result") == "LOSS")
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
        "winrate": round(wins / len(rows) * 100, 2) if rows else 0.0,
        "profit_factor": profit_factor(pnls),
        "net_profit": round(sum(pnls), 8),
        "gross_profit": round(sum(value for value in pnls if value > 0), 8),
        "gross_loss": round(sum(value for value in pnls if value < 0), 8),
    }


def signal_after_downgrade(signal: str) -> str:
    """Return one-step downgraded signal."""
    return DOWNGRADE_MAP.get(signal, signal)


class TrendMomentumConflictExperiment:
    """Run read-only conflict rule experiment on closed trades."""

    def __init__(self) -> None:
        self.trades = read_csv_rows(TRADES_FILE)
        self.debug_by_symbol = group_by_symbol(read_csv_rows(DEBUG_FILE))
        self.diagnostics_by_symbol = group_by_symbol(read_csv_rows(DIAGNOSTICS_FILE))

    def build_report(self) -> Dict[str, Any]:
        """Build all experiment artifacts."""
        analyzed = [self.analyze_trade(row) for row in self.closed_trades()]
        baseline = metrics(analyzed)
        scenarios = {
            "downgrade_one_step": self.scenario_metrics(analyzed, "downgrade_one_step"),
            "block_to_no_trade": self.scenario_metrics(analyzed, "block_to_no_trade"),
        }
        conflict_rows = [row for row in analyzed if row["conflict_rule_match"]]
        report = {
            "generated_at": utc_now(),
            "status": "OK" if analyzed else "NO_CLOSED_TRADES",
            "rule": {
                "direction": "SHORT",
                "one_hour_ema_trend": "BULLISH",
                "momentum": "FAIL",
                "downgrade": "HIGH PRIORITY -> SETUP, SETUP -> WATCH",
                "hard_block": "matching signals -> NO TRADE",
                "trade_eligibility_assumption": "Only SETUP and HIGH PRIORITY are entry-eligible.",
            },
            "baseline": baseline,
            "scenarios": scenarios,
            "conflict_summary": self.conflict_summary(conflict_rows),
            "recommendation": self.recommendation(baseline, scenarios),
            "trades": analyzed,
        }
        write_json(REPORT_JSON, report)
        self.write_trades_csv(analyzed)
        self.write_summary(report)
        return report

    def closed_trades(self) -> List[Dict[str, str]]:
        """Return closed trades with WIN/LOSS result."""
        rows = []
        for row in self.trades:
            result = str(row.get("result") or row.get("status", "")).upper()
            if result in {"WIN", "LOSS"}:
                rows.append(row)
        return rows

    def analyze_trade(self, trade: Mapping[str, str]) -> Dict[str, Any]:
        """Attach decision context and rule result to a closed trade."""
        symbol = trade.get("symbol", "")
        direction = str(trade.get("direction", "")).upper()
        opened_at = parse_time(trade.get("opened_at"))
        debug_row = nearest_before(
            self.debug_by_symbol.get(symbol, []),
            opened_at,
            direction=direction,
        )
        decision_time = parse_time(debug_row.get("timestamp")) if debug_row else opened_at
        diagnostic_row = nearest_before(
            self.diagnostics_by_symbol.get(symbol, []),
            decision_time,
        )
        signal = str(debug_row.get("signal", "") if debug_row else "").upper()
        one_hour_bullish = self.is_one_hour_ema_bullish(debug_row)
        momentum_fail = self.is_momentum_fail(debug_row, diagnostic_row, direction)
        conflict = direction == "SHORT" and one_hour_bullish and momentum_fail
        downgraded_signal = signal_after_downgrade(signal) if conflict else signal
        hard_block_signal = "NO TRADE" if conflict else signal
        result = str(trade.get("result") or trade.get("status", "")).upper()
        pnl = infer_pnl(trade)
        return {
            "symbol": symbol,
            "direction": direction,
            "result": result,
            "pnl": pnl,
            "entry": safe_float(trade.get("entry")),
            "stop_loss": safe_float(trade.get("stop_loss")),
            "take_profit": safe_float(trade.get("take_profit")),
            "opened_at": trade.get("opened_at", ""),
            "closed_at": trade.get("closed_at", ""),
            "decision_time": debug_row.get("timestamp", "") if debug_row else "",
            "baseline_signal": signal,
            "score": safe_float(debug_row.get("score")) if debug_row else 0.0,
            "confidence": safe_float(debug_row.get("confidence")) if debug_row else 0.0,
            "quality": debug_row.get("quality", "") if debug_row else "",
            "primary_blocker": diagnostic_row.get("primary_blocker", "") if diagnostic_row else "",
            "momentum_status": diagnostic_row.get("momentum", "") if diagnostic_row else "",
            "trend_reason": debug_row.get("trend_reason", "") if debug_row else "",
            "one_hour_ema_bullish": one_hour_bullish,
            "momentum_fail": momentum_fail,
            "conflict_rule_match": conflict,
            "downgrade_one_step_signal": downgraded_signal,
            "block_to_no_trade_signal": hard_block_signal,
            "downgrade_prevented": signal in TRADE_SIGNALS and downgraded_signal not in TRADE_SIGNALS,
            "block_prevented": signal in TRADE_SIGNALS and hard_block_signal not in TRADE_SIGNALS,
        }

    @staticmethod
    def is_one_hour_ema_bullish(debug_row: Optional[Mapping[str, str]]) -> bool:
        """Detect 1h EMA bullish state from decision_debug trend_reason."""
        if debug_row is None:
            return False
        trend_reason = str(debug_row.get("trend_reason", ""))
        return "1h: EMA=BULLISH" in trend_reason

    @staticmethod
    def is_momentum_fail(
        debug_row: Optional[Mapping[str, str]],
        diagnostic_row: Optional[Mapping[str, str]],
        direction: str,
    ) -> bool:
        """Detect Momentum FAIL from diagnostics or direction-specific scores."""
        if diagnostic_row is not None:
            status = str(diagnostic_row.get("momentum", "")).upper()
            if status == "FAIL":
                return True
            if status == "PASS":
                return False
        if debug_row is None or direction not in {"LONG", "SHORT"}:
            return False
        selected = contribution(debug_row, direction, "momentum")
        opposite = contribution(debug_row, opposite_direction(direction), "momentum")
        return selected <= 0 or selected < opposite

    @staticmethod
    def scenario_metrics(
        rows: Sequence[Mapping[str, Any]],
        scenario: str,
    ) -> Dict[str, Any]:
        """Calculate metrics after applying scenario filter."""
        prevented_field = "downgrade_prevented"
        if scenario == "block_to_no_trade":
            prevented_field = "block_prevented"
        prevented = [row for row in rows if row.get(prevented_field)]
        remaining = [row for row in rows if not row.get(prevented_field)]
        prevented_wins = [row for row in prevented if row.get("result") == "WIN"]
        prevented_losses = [row for row in prevented if row.get("result") == "LOSS"]
        payload = metrics(remaining)
        payload.update(
            {
                "prevented_trades": len(prevented),
                "prevented_losses": len(prevented_losses),
                "lost_wins": len(prevented_wins),
                "prevented_loss_pnl": round(sum(safe_float(row.get("pnl")) for row in prevented_losses), 8),
                "lost_win_pnl": round(sum(safe_float(row.get("pnl")) for row in prevented_wins), 8),
                "net_profit_delta_vs_baseline": round(
                    sum(safe_float(row.get("pnl")) for row in remaining)
                    - sum(safe_float(row.get("pnl")) for row in rows),
                    8,
                ),
            }
        )
        return payload

    @staticmethod
    def conflict_summary(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """Summarize matching conflict trades."""
        result_counter = Counter(row.get("result") for row in rows)
        symbol_counter = Counter(row.get("symbol") for row in rows)
        return {
            "matches": len(rows),
            "wins": result_counter.get("WIN", 0),
            "losses": result_counter.get("LOSS", 0),
            "net_profit": round(sum(safe_float(row.get("pnl")) for row in rows), 8),
            "symbols": dict(symbol_counter.most_common()),
        }

    @staticmethod
    def recommendation(
        baseline: Mapping[str, Any],
        scenarios: Mapping[str, Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Generate read-only recommendation from experiment results."""
        best_name = ""
        best_payload: Mapping[str, Any] = {}
        for name, payload in scenarios.items():
            if not best_payload or safe_float(payload.get("net_profit_delta_vs_baseline")) > safe_float(
                best_payload.get("net_profit_delta_vs_baseline")
            ):
                best_name = name
                best_payload = payload

        prevented_losses = safe_float(best_payload.get("prevented_losses"))
        lost_wins = safe_float(best_payload.get("lost_wins"))
        improved_pf = safe_float(best_payload.get("profit_factor")) >= safe_float(
            baseline.get("profit_factor")
        )
        improved_net = safe_float(best_payload.get("net_profit_delta_vs_baseline")) > 0
        if prevented_losses > lost_wins and improved_net and improved_pf:
            status = "CANDIDATE_FOR_V2_TEST"
            reason = "Rule prevented more losses than wins and improved PF/net profit in historical trades."
        elif prevented_losses > lost_wins:
            status = "NEEDS_MORE_TESTING"
            reason = "Rule prevented more losses than wins, but did not clearly improve all metrics."
        else:
            status = "NOT_RECOMMENDED"
            reason = "Rule did not prevent more losing trades than winning trades."
        return {
            "status": status,
            "best_scenario": best_name,
            "reason": reason,
            "apply_automatically": False,
            "baseline_trades": baseline.get("trades", 0),
            "closed_trade_sample_warning": (
                "This is a read-only closed-trade experiment, not a live DecisionEngine change."
            ),
        }

    @staticmethod
    def write_trades_csv(rows: Sequence[Mapping[str, Any]]) -> None:
        """Write per-trade experiment output."""
        fieldnames = [
            "symbol",
            "direction",
            "result",
            "pnl",
            "opened_at",
            "closed_at",
            "decision_time",
            "baseline_signal",
            "score",
            "confidence",
            "quality",
            "primary_blocker",
            "momentum_status",
            "one_hour_ema_bullish",
            "momentum_fail",
            "conflict_rule_match",
            "downgrade_one_step_signal",
            "block_to_no_trade_signal",
            "downgrade_prevented",
            "block_prevented",
        ]
        with TRADES_CSV.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in fieldnames})

    @staticmethod
    def write_summary(report: Mapping[str, Any]) -> None:
        """Write compact text summary."""
        baseline = report.get("baseline", {})
        scenarios = report.get("scenarios", {})
        conflict = report.get("conflict_summary", {})
        rec = report.get("recommendation", {})
        lines = [
            "Trend/Momentum Conflict Experiment",
            "==================================",
            f"Generated: {report.get('generated_at')}",
            "",
            "Rule:",
            "- IF direction = SHORT",
            "- AND 1H EMA trend = BULLISH",
            "- AND Momentum = FAIL",
            "- THEN downgrade signal or block to NO TRADE",
            "",
            "Baseline:",
            f"- Trades: {baseline.get('trades')}",
            f"- Wins/Losses: {baseline.get('wins')} / {baseline.get('losses')}",
            f"- Winrate: {baseline.get('winrate')}%",
            f"- Profit Factor: {baseline.get('profit_factor')}",
            f"- Net Profit: {baseline.get('net_profit')}",
            "",
            "Conflict matches:",
            f"- Matches: {conflict.get('matches')}",
            f"- Wins/Losses: {conflict.get('wins')} / {conflict.get('losses')}",
            f"- Net Profit: {conflict.get('net_profit')}",
            f"- Symbols: {conflict.get('symbols')}",
            "",
            "Scenarios:",
        ]
        for name, payload in scenarios.items():
            lines.extend(
                [
                    f"- {name}:",
                    f"  trades={payload.get('trades')} wins={payload.get('wins')} losses={payload.get('losses')}",
                    f"  winrate={payload.get('winrate')}% PF={payload.get('profit_factor')} net={payload.get('net_profit')}",
                    f"  prevented_losses={payload.get('prevented_losses')} lost_wins={payload.get('lost_wins')}",
                    f"  net_delta={payload.get('net_profit_delta_vs_baseline')}",
                ]
            )
        lines.extend(
            [
                "",
                "Recommendation:",
                f"- Status: {rec.get('status')}",
                f"- Best scenario: {rec.get('best_scenario')}",
                f"- Reason: {rec.get('reason')}",
                "- Apply automatically: false",
            ]
        )
        SUMMARY_TEXT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def print_report(report: Mapping[str, Any]) -> None:
        """Print terminal summary."""
        baseline = report.get("baseline", {})
        conflict = report.get("conflict_summary", {})
        rec = report.get("recommendation", {})
        print("Trend/Momentum Conflict Experiment")
        print(f"Baseline trades: {baseline.get('trades')}")
        print(f"Baseline Winrate/PF/Net: {baseline.get('winrate')}% / {baseline.get('profit_factor')} / {baseline.get('net_profit')}")
        print(f"Conflict matches: {conflict.get('matches')} | wins={conflict.get('wins')} losses={conflict.get('losses')}")
        for name, payload in report.get("scenarios", {}).items():
            print(
                f"{name}: prevented_losses={payload.get('prevented_losses')} "
                f"lost_wins={payload.get('lost_wins')} "
                f"PF={payload.get('profit_factor')} "
                f"net_delta={payload.get('net_profit_delta_vs_baseline')}"
            )
        print(f"Recommendation: {rec.get('status')} ({rec.get('best_scenario')})")
        print(f"JSON: {REPORT_JSON.name}")
        print(f"Summary: {SUMMARY_TEXT.name}")
        print(f"CSV: {TRADES_CSV.name}")


def main() -> None:
    """CLI entry point."""
    experiment = TrendMomentumConflictExperiment()
    report = experiment.build_report()
    experiment.print_report(report)


if __name__ == "__main__":
    main()
