"""Filter effectiveness analytics for AITradingAgent."""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


BASE_DIR = Path(__file__).resolve().parent
TRADES_FILE = BASE_DIR / "trades.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"

JSON_OUTPUT = BASE_DIR / "filter_effectiveness_report.json"
TEXT_OUTPUT = BASE_DIR / "filter_effectiveness_summary.txt"

FILTERS = ("Trend", "Structure", "Momentum", "Risk")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def parse_time(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    return [
        row for row in rows
        if row and any(row.values()) and row.get("timestamp") != "timestamp"
    ]


def candidate_direction_from_debug(row: Mapping[str, str]) -> str:
    direction = row.get("direction", "")
    if direction in {"LONG", "SHORT"}:
        return direction
    long_total = safe_float(row.get("long_total"))
    short_total = safe_float(row.get("short_total"))
    if long_total > short_total:
        return "LONG"
    if short_total > long_total:
        return "SHORT"
    return "NEUTRAL"


def compute_trade_metrics(trades: List[Mapping[str, Any]]) -> Dict[str, float]:
    wins = sum(1 for trade in trades if trade.get("status") == "WIN")
    losses = sum(1 for trade in trades if trade.get("status") == "LOSS")
    pnls = [safe_float(trade.get("pnl")) for trade in trades]
    gross_profit = sum(max(pnl, 0.0) for pnl in pnls)
    gross_loss = sum(abs(min(pnl, 0.0)) for pnl in pnls)
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    return {
        "closed_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "winrate": round((wins / len(trades) * 100) if trades else 0.0, 2),
        "profit_factor": round((gross_profit / gross_loss) if gross_loss else 0.0, 2),
        "average_pnl": round((sum(pnls) / len(pnls)) if pnls else 0.0, 2),
        "max_drawdown": round(max_drawdown, 2),
    }


def nearest_debug_row(
    symbol: str,
    timestamp: datetime,
    debug_by_symbol: Mapping[str, List[Dict[str, str]]],
) -> Optional[Dict[str, str]]:
    best_row: Optional[Dict[str, str]] = None
    best_delta: Optional[float] = None
    for row in debug_by_symbol.get(symbol, []):
        row_time = parse_time(row.get("timestamp", ""))
        if row_time is None:
            continue
        delta = abs((row_time - timestamp).total_seconds())
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_row = row
    return best_row


def next_trade_outcome(
    symbol: str,
    direction: str,
    timestamp: datetime,
    trades_by_symbol: Mapping[str, List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    for trade in trades_by_symbol.get(symbol, []):
        opened_at = trade.get("_opened_at")
        if opened_at is None or opened_at < timestamp:
            continue
        if trade.get("direction") != direction:
            continue
        return trade
    return None


def build_report() -> Dict[str, Any]:
    diagnostics_rows = read_csv_rows(DIAGNOSTICS_FILE)
    debug_rows = read_csv_rows(DEBUG_FILE)
    signal_rows = read_csv_rows(SIGNALS_FILE)
    trade_rows = [
        row for row in read_csv_rows(TRADES_FILE)
        if (row.get("status") or row.get("result")) in {"WIN", "LOSS"}
    ]

    for trade in trade_rows:
        trade["status"] = trade.get("status") or trade.get("result")
        trade["_opened_at"] = parse_time(trade.get("opened_at", ""))

    baseline_candidates = [
        row for row in signal_rows
        if row.get("signal") in {"SETUP", "HIGH PRIORITY"}
    ]
    baseline_trade_metrics = compute_trade_metrics(trade_rows)

    debug_by_symbol: Dict[str, List[Dict[str, str]]] = {}
    for row in debug_rows:
        debug_by_symbol.setdefault(row.get("symbol", ""), []).append(row)
    for rows in debug_by_symbol.values():
        rows.sort(key=lambda row: row.get("timestamp", ""))

    trades_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for trade in trade_rows:
        trades_by_symbol.setdefault(trade.get("symbol", ""), []).append(trade)
    for rows in trades_by_symbol.values():
        rows.sort(key=lambda row: row.get("_opened_at") or datetime.min.replace(tzinfo=timezone.utc))

    filter_stats: Dict[str, Dict[str, Any]] = {}
    blocker_counter = Counter(row.get("primary_blocker", "") for row in diagnostics_rows)

    for filter_name in FILTERS:
        key = filter_name.lower()
        pass_count = sum(1 for row in diagnostics_rows if row.get(key) == "PASS")
        fail_rows = [row for row in diagnostics_rows if row.get(key) == "FAIL"]
        blocked_outcomes: List[Dict[str, Any]] = []

        for row in fail_rows:
            ts = parse_time(row.get("timestamp", ""))
            symbol = row.get("symbol", "")
            if ts is None or not symbol:
                continue
            debug_row = nearest_debug_row(symbol, ts, debug_by_symbol)
            if debug_row is None:
                continue
            direction = candidate_direction_from_debug(debug_row)
            if direction == "NEUTRAL":
                continue
            trade = next_trade_outcome(symbol, direction, ts, trades_by_symbol)
            if trade is not None:
                blocked_outcomes.append(trade)

        blocked_metrics = compute_trade_metrics(blocked_outcomes)
        blocked_profitable = sum(1 for trade in blocked_outcomes if trade.get("status") == "WIN")
        blocked_losing = sum(1 for trade in blocked_outcomes if trade.get("status") == "LOSS")
        combined_metrics = compute_trade_metrics(trade_rows + blocked_outcomes)

        filter_stats[filter_name] = {
            "pass_count": pass_count,
            "fail_count": len(fail_rows),
            "blocked_potential_trades": len(fail_rows),
            "matched_blocked_trades": len(blocked_outcomes),
            "blocked_profitable": blocked_profitable,
            "blocked_losing": blocked_losing,
            "blocked_average_pnl": blocked_metrics["average_pnl"],
            "blocked_winrate": blocked_metrics["winrate"],
            "blocked_profit_factor": blocked_metrics["profit_factor"],
            "blocked_max_drawdown": blocked_metrics["max_drawdown"],
            "winrate_impact": round(combined_metrics["winrate"] - baseline_trade_metrics["winrate"], 2),
            "profit_factor_impact": round(combined_metrics["profit_factor"] - baseline_trade_metrics["profit_factor"], 2),
            "drawdown_impact": round(combined_metrics["max_drawdown"] - baseline_trade_metrics["max_drawdown"], 2),
        }

    filters_with_matches = [
        name for name in FILTERS
        if filter_stats[name]["matched_blocked_trades"] > 0
    ]
    useful_filter = "N/A"
    harmful_filter = "N/A"
    if filters_with_matches:
        useful_filter = min(
            filters_with_matches,
            key=lambda name: (
                filter_stats[name]["blocked_average_pnl"],
                filter_stats[name]["profit_factor_impact"],
            ),
        )
        harmful_filter = max(
            filters_with_matches,
            key=lambda name: (
                filter_stats[name]["blocked_average_pnl"],
                filter_stats[name]["profit_factor_impact"],
            ),
        )
    strict_filter = max(FILTERS, key=lambda name: filter_stats[name]["fail_count"])
    useless_filter = max(
        FILTERS,
        key=lambda name: (
            filter_stats[name]["matched_blocked_trades"] == 0,
            filter_stats[name]["fail_count"],
        ),
    )

    recommendations = [
        f"Главный блокирующий фильтр сейчас: {blocker_counter.most_common(1)[0][0]}."
        if blocker_counter else "Недостаточно diagnostics-данных для главного блокера.",
        (
            f"{strict_filter} сейчас самый строгий фильтр по числу FAIL. "
            "Его стоит проверять первым в research/backtest."
        ),
        (
            f"{useless_filter} пока выглядит самым бесполезным по текущему окну: "
            "много FAIL, но пока нет подтверждённой trade-связки для оценки эффекта."
        ),
    ]
    if filters_with_matches:
        recommendations.extend(
            [
                (
                    f"{harmful_filter} выглядит самым вредным фильтром: "
                    "он чаще блокирует сделки, которые по исторической оценке выглядели лучше baseline."
                ),
                (
                    f"{useful_filter} выглядит самым полезным фильтром: "
                    "его блокировки чаще совпадают с более слабыми исходами."
                ),
            ]
        )
    else:
        recommendations.append(
            "Для денежной оценки полезности/вредности пока не хватает сопоставленных blocked-trade исходов."
        )

    return {
        "generated_at": utc_now(),
        "notes": [
            "Blocked trade outcomes are estimated analytically from the nearest later closed trade with the same symbol and direction.",
            "This report is advisory only and does not change DecisionEngine or live weights.",
        ],
        "baseline": {
            "actionable_signals": len(baseline_candidates),
            "trade_metrics": baseline_trade_metrics,
        },
        "filters": filter_stats,
        "ranking": {
            "most_useful": useful_filter,
            "most_harmful": harmful_filter,
            "most_strict": strict_filter,
            "most_useless": useless_filter,
            "main_blocker": blocker_counter.most_common(1)[0][0] if blocker_counter else "N/A",
        },
        "recommendations": recommendations,
    }


def save_report(report: Dict[str, Any]) -> None:
    with JSON_OUTPUT.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)


def save_summary(report: Dict[str, Any]) -> None:
    lines = [
        "AITradingAgent Filter Effectiveness",
        f"Generated at: {report.get('generated_at', '')}",
        "",
        f"Most useful: {report.get('ranking', {}).get('most_useful', 'N/A')}",
        f"Most harmful: {report.get('ranking', {}).get('most_harmful', 'N/A')}",
        f"Most strict: {report.get('ranking', {}).get('most_strict', 'N/A')}",
        f"Most useless: {report.get('ranking', {}).get('most_useless', 'N/A')}",
        f"Main blocker: {report.get('ranking', {}).get('main_blocker', 'N/A')}",
        "",
        "Recommendations:",
    ]
    for item in report.get("recommendations", []):
        lines.append(f"- {item}")
    TEXT_OUTPUT.write_text("\n".join(lines), encoding="utf-8")


def print_summary(report: Dict[str, Any]) -> None:
    print("Filter Effectiveness")
    print(f"Most useful     : {report.get('ranking', {}).get('most_useful', 'N/A')}")
    print(f"Most harmful    : {report.get('ranking', {}).get('most_harmful', 'N/A')}")
    print(f"Main blocker    : {report.get('ranking', {}).get('main_blocker', 'N/A')}")
    print(f"JSON report     : {JSON_OUTPUT}")
    print(f"TXT summary     : {TEXT_OUTPUT}")


def main() -> None:
    report = build_report()
    save_report(report)
    save_summary(report)
    print_summary(report)


if __name__ == "__main__":
    main()
