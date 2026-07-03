"""Isolated strategy experiments for historical backtest-style scenarios."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from multi_timeframe_agent_v3 import DecisionEngine


BASE_DIR = Path(__file__).resolve().parent
DECISION_DEBUG_FILE = BASE_DIR / "decision_debug.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
TRADES_FILE = BASE_DIR / "trades.csv"
WEIGHTS_FILE = BASE_DIR / "strategy_weights.json"

JSON_OUTPUT_FILE = BASE_DIR / "strategy_experiments_report.json"
TEXT_OUTPUT_FILE = BASE_DIR / "strategy_experiments_summary.txt"
CSV_OUTPUT_FILE = BASE_DIR / "strategy_experiments_results.csv"

@dataclass(frozen=True)
class Scenario:
    """Experiment scenario definition."""

    name: str
    mode: str
    overrides: Dict[str, float]
    symbol_filter: Optional[str] = None


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read CSV rows while ignoring empty rows and repeated headers."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    clean_rows: List[Dict[str, str]] = []
    for row in rows:
        if not row or not any(row.values()):
            continue
        if row.get("timestamp") == "timestamp":
            continue
        clean_rows.append(row)
    return clean_rows


def read_json(path: Path) -> Dict[str, Any]:
    """Read a JSON object from disk."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert mixed CSV values to float."""
    try:
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def parse_time(value: str) -> Optional[datetime]:
    """Parse ISO timestamps used in the project logs."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_weights_map() -> Dict[str, Dict[str, float]]:
    """Load baseline strategy weights."""
    payload = read_json(WEIGHTS_FILE)
    weights_map: Dict[str, Dict[str, float]] = {}
    for key, value in payload.items():
        if not isinstance(value, Mapping):
            continue
        weights_map[key] = {
            "trend": safe_float(value.get("trend"), 0.4),
            "structure": safe_float(value.get("structure"), 0.25),
            "momentum": safe_float(value.get("momentum"), 0.2),
            "risk": safe_float(value.get("risk"), 0.15),
        }
    if "global" not in weights_map:
        weights_map["global"] = {
            "trend": 0.4,
            "structure": 0.25,
            "momentum": 0.2,
            "risk": 0.15,
        }
    return weights_map


def baseline_weights(symbol: str, weights_map: Mapping[str, Dict[str, float]]) -> Dict[str, float]:
    """Return baseline weights for a symbol."""
    return dict(weights_map.get(symbol) or weights_map.get("global") or {})


def build_trade_links(
    decision_rows: Iterable[Mapping[str, str]],
    trade_rows: Iterable[Mapping[str, str]],
) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    """Attach closed trade outcomes to the nearest prior decision row."""
    signals_by_symbol: Dict[str, List[Dict[str, str]]] = {}
    for row in decision_rows:
        symbol = row.get("symbol", "")
        if not symbol:
            continue
        signals_by_symbol.setdefault(symbol, []).append(dict(row))

    for symbol_rows in signals_by_symbol.values():
        symbol_rows.sort(key=lambda row: row.get("timestamp", ""))

    links: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for trade in trade_rows:
        status = trade.get("status") or trade.get("result")
        if status not in {"WIN", "LOSS"}:
            continue
        symbol = trade.get("symbol", "")
        direction = trade.get("direction", "")
        opened_at = parse_time(trade.get("opened_at", ""))
        if not symbol or not direction or opened_at is None:
            continue

        matched_signal: Optional[Dict[str, str]] = None
        for signal in signals_by_symbol.get(symbol, []):
            signal_time = parse_time(signal.get("timestamp", ""))
            if signal_time is None or signal_time > opened_at:
                continue
            if signal.get("direction") != direction:
                continue
            matched_signal = signal

        if matched_signal is None:
            continue

        key = (
            matched_signal.get("timestamp", ""),
            matched_signal.get("symbol", ""),
        )
        links.setdefault(key, []).append(
            {
                "status": status,
                "pnl": safe_float(trade.get("pnl")),
                "direction": direction,
                "symbol": symbol,
            }
        )
    return links


def scenario_list() -> List[Scenario]:
    """Return all experiment scenarios."""
    scenarios = [Scenario("baseline", "baseline", {})]
    scenarios.append(Scenario("momentum_soft", "momentum_soft", {}))
    for value in (0.35, 0.40, 0.45):
        scenarios.append(
            Scenario(
                name=f"trend_weight_{value:.2f}",
                mode="weight_override",
                overrides={"trend": value},
            )
        )
    for value in (0.20, 0.25, 0.30):
        scenarios.append(
            Scenario(
                name=f"structure_weight_{value:.2f}",
                mode="weight_override",
                overrides={"structure": value},
            )
        )
    for value in (0.10, 0.15, 0.20):
        scenarios.append(
            Scenario(
                name=f"risk_weight_{value:.2f}",
                mode="weight_override",
                overrides={"risk": value},
            )
        )
    scenarios.append(Scenario("short_bias_check", "short_bias_check", {}))
    for symbol in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
        scenarios.append(
            Scenario(
                name=f"symbol_weights_{symbol.replace('/USDT', '')}",
                mode="symbol_filter",
                overrides={},
                symbol_filter=symbol,
            )
        )
    return scenarios


def adjusted_engines(
    row: Mapping[str, str],
    scenario: Scenario,
) -> Tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    """Build engine inputs for a scenario from decision_debug rows."""
    trend = SimpleNamespace(
        long=safe_float(row.get("trend_long")),
        short=safe_float(row.get("trend_short")),
    )
    structure = SimpleNamespace(
        long=safe_float(row.get("structure_long")),
        short=safe_float(row.get("structure_short")),
    )
    momentum = SimpleNamespace(
        long=safe_float(row.get("momentum_long")),
        short=safe_float(row.get("momentum_short")),
    )
    risk = SimpleNamespace(
        long=safe_float(row.get("risk_long")),
        short=safe_float(row.get("risk_short")),
    )

    if scenario.mode == "momentum_soft":
        if momentum.long >= momentum.short:
            momentum.long = round(momentum.long * 1.20 + 1.0, 2)
            momentum.short = round(momentum.short * 1.05, 2)
        else:
            momentum.short = round(momentum.short * 1.20 + 1.0, 2)
            momentum.long = round(momentum.long * 1.05, 2)
    elif scenario.mode == "short_bias_check":
        momentum.short = round(momentum.short * 1.10, 2)
        trend.short = round(trend.short * 1.05, 2)

    return trend, structure, momentum, risk


def scenario_weights(
    symbol: str,
    scenario: Scenario,
    weights_map: Mapping[str, Dict[str, float]],
) -> Dict[str, float]:
    """Return scenario-specific weights without mutating the source file."""
    weights = baseline_weights(symbol, weights_map)
    if scenario.mode == "weight_override":
        weights.update(scenario.overrides)
    return weights


def signal_counts(rows: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    """Count decisions by signal label."""
    counts: Dict[str, int] = {}
    for row in rows:
        signal = str(row.get("signal", "UNKNOWN"))
        counts[signal] = counts.get(signal, 0) + 1
    return counts


def compute_max_drawdown(pnls: List[float]) -> float:
    """Compute max drawdown from cumulative pnl."""
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    return round(max_drawdown, 2)


def summarize_trades(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build trade metrics for a scenario."""
    wins = sum(1 for trade in trades if trade["status"] == "WIN")
    losses = sum(1 for trade in trades if trade["status"] == "LOSS")
    pnls = [trade["pnl"] for trade in trades]
    gross_profit = sum(max(pnl, 0.0) for pnl in pnls)
    gross_loss = sum(abs(min(pnl, 0.0)) for pnl in pnls)
    profit_factor = gross_profit / gross_loss if gross_loss else 0.0
    average_pnl = sum(pnls) / len(pnls) if pnls else 0.0
    long_count = sum(1 for trade in trades if trade["direction"] == "LONG")
    short_count = sum(1 for trade in trades if trade["direction"] == "SHORT")
    return {
        "closed_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "winrate": round((wins / len(trades) * 100) if trades else 0.0, 2),
        "average_pnl": round(average_pnl, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown": compute_max_drawdown(pnls),
        "long_trades": long_count,
        "short_trades": short_count,
    }


def evaluate_scenario(
    scenario: Scenario,
    debug_rows: Iterable[Mapping[str, str]],
    weights_map: Mapping[str, Dict[str, float]],
    trade_links: Mapping[Tuple[str, str], List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Run a single scenario against historical decision debug rows."""
    decisions: List[Dict[str, Any]] = []
    matched_trades: List[Dict[str, Any]] = []

    for row in debug_rows:
        symbol = row.get("symbol", "")
        if scenario.symbol_filter and symbol != scenario.symbol_filter:
            continue

        trend, structure, momentum, risk = adjusted_engines(row, scenario)
        weights = scenario_weights(symbol, scenario, weights_map)
        decision = DecisionEngine.calculate(
            trend,
            structure,
            momentum,
            risk,
            weights,
        )
        decisions.append(
            {
                "timestamp": row.get("timestamp", ""),
                "symbol": symbol,
                "direction": decision.direction,
                "signal": decision.signal,
                "score": decision.score,
                "confidence": decision.confidence,
            }
        )

        if decision.signal != "NO TRADE" and decision.direction != "NEUTRAL":
            matched_trades.extend(
                trade_links.get((row.get("timestamp", ""), symbol), [])
            )

    counts = signal_counts(decisions)
    trade_summary = summarize_trades(matched_trades)
    setup_count = counts.get("SETUP", 0)
    high_priority_count = counts.get("HIGH PRIORITY", 0)
    no_trade_count = counts.get("NO TRADE", 0)

    return {
        "scenario": scenario.name,
        "mode": scenario.mode,
        "symbol_scope": scenario.symbol_filter or "ALL",
        "weights_override": scenario.overrides,
        "decision_count": len(decisions),
        "signals_count": len(
            [item for item in decisions if item["signal"] != "NO TRADE"]
        ),
        "setup_count": setup_count,
        "high_priority_count": high_priority_count,
        "no_trade_count": no_trade_count,
        "signal_breakdown": counts,
        "trade_metrics": trade_summary,
    }


def rank_scenarios(results: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return best and worst scenarios."""
    ranked = sorted(
        results,
        key=lambda item: (
            safe_float(item["trade_metrics"].get("profit_factor")),
            safe_float(item["trade_metrics"].get("average_pnl")),
            safe_float(item["trade_metrics"].get("winrate")),
            safe_float(item.get("signals_count")),
        ),
        reverse=True,
    )
    return ranked[0], ranked[-1]


def build_report() -> Dict[str, Any]:
    """Build the complete strategy experiments report."""
    debug_rows = read_csv_rows(DECISION_DEBUG_FILE)
    trade_rows = read_csv_rows(TRADES_FILE)
    weights_map = load_weights_map()
    trade_links = build_trade_links(debug_rows, trade_rows)

    results = [
        evaluate_scenario(scenario, debug_rows, weights_map, trade_links)
        for scenario in scenario_list()
    ]
    best, worst = rank_scenarios(results)

    return {
        "generated_at": utc_now(),
        "source_files": {
            "decision_debug": str(DECISION_DEBUG_FILE),
            "signals": str(SIGNALS_FILE),
            "trades": str(TRADES_FILE),
            "weights": str(WEIGHTS_FILE),
        },
        "notes": [
            (
                "Scenarios are isolated and do not modify DecisionEngine, "
                "multi_timeframe_agent_v3.py, or strategy_weights.json."
            ),
            (
                "Trade metrics are estimated from recorded closed trades "
                "matched to historical signals by symbol, direction, and time."
            ),
        ],
        "scenario_count": len(results),
        "results": results,
        "best_scenario": best,
        "worst_scenario": worst,
    }


def save_json_report(report: Dict[str, Any]) -> None:
    """Write the JSON report."""
    with JSON_OUTPUT_FILE.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)


def save_csv_results(report: Dict[str, Any]) -> None:
    """Write flat CSV results for quick comparison."""
    rows = report.get("results", [])
    with CSV_OUTPUT_FILE.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "scenario",
                "mode",
                "symbol_scope",
                "decision_count",
                "signals_count",
                "setup_count",
                "high_priority_count",
                "no_trade_count",
                "closed_trades",
                "winrate",
                "average_pnl",
                "profit_factor",
                "max_drawdown",
                "long_trades",
                "short_trades",
            ]
        )
        for row in rows:
            metrics = row.get("trade_metrics", {})
            writer.writerow(
                [
                    row.get("scenario", ""),
                    row.get("mode", ""),
                    row.get("symbol_scope", ""),
                    row.get("decision_count", 0),
                    row.get("signals_count", 0),
                    row.get("setup_count", 0),
                    row.get("high_priority_count", 0),
                    row.get("no_trade_count", 0),
                    metrics.get("closed_trades", 0),
                    metrics.get("winrate", 0),
                    metrics.get("average_pnl", 0),
                    metrics.get("profit_factor", 0),
                    metrics.get("max_drawdown", 0),
                    metrics.get("long_trades", 0),
                    metrics.get("short_trades", 0),
                ]
            )


def save_text_summary(report: Dict[str, Any]) -> None:
    """Write a compact text summary."""
    best = report.get("best_scenario", {})
    worst = report.get("worst_scenario", {})
    lines = [
        "AITradingAgent Strategy Experiments",
        f"Generated at: {report.get('generated_at', '')}",
        "",
        f"Scenarios tested: {report.get('scenario_count', 0)}",
        "",
        "Best scenario",
        format_summary_row(best),
        "",
        "Worst scenario",
        format_summary_row(worst),
        "",
        "All scenarios",
    ]
    for row in report.get("results", []):
        lines.append(format_summary_row(row))
    TEXT_OUTPUT_FILE.write_text("\n".join(lines), encoding="utf-8")


def format_summary_row(row: Mapping[str, Any]) -> str:
    """Format one scenario row for text output."""
    metrics = row.get("trade_metrics", {})
    return (
        f"- {row.get('scenario', 'N/A')}: "
        f"signals={row.get('signals_count', 0)}, "
        f"SETUP={row.get('setup_count', 0)}, "
        f"HIGH PRIORITY={row.get('high_priority_count', 0)}, "
        f"NO TRADE={row.get('no_trade_count', 0)}, "
        f"winrate={metrics.get('winrate', 0)}%, "
        f"avg_pnl={metrics.get('average_pnl', 0)}, "
        f"pf={metrics.get('profit_factor', 0)}, "
        f"max_dd={metrics.get('max_drawdown', 0)}"
    )


def print_summary(report: Dict[str, Any]) -> None:
    """Print a concise console summary."""
    best = report.get("best_scenario", {})
    worst = report.get("worst_scenario", {})
    print("Strategy Experiments")
    print(f"Scenarios tested : {report.get('scenario_count', 0)}")
    print(f"Best scenario    : {best.get('scenario', 'N/A')}")
    print(f"Worst scenario   : {worst.get('scenario', 'N/A')}")
    print(f"JSON report      : {JSON_OUTPUT_FILE}")
    print(f"TXT summary      : {TEXT_OUTPUT_FILE}")
    print(f"CSV results      : {CSV_OUTPUT_FILE}")


def main() -> None:
    """Run isolated strategy experiments."""
    report = build_report()
    save_json_report(report)
    save_text_summary(report)
    save_csv_results(report)
    print_summary(report)


if __name__ == "__main__":
    main()
