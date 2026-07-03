"""Strategy research report for AITradingAgent v1.0.

This module analyzes stored diagnostics, debug logs, signals, trades, and
weights to suggest research directions. It does not change trading logic,
DecisionEngine, config values, or strategy weights.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from market_regime import build_market_regime_report


BASE_DIR = Path(__file__).resolve().parent
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
DECISION_DEBUG_FILE = BASE_DIR / "decision_debug.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
TRADES_FILE = BASE_DIR / "trades.csv"
WEIGHTS_FILE = BASE_DIR / "strategy_weights.json"
MARKET_REGIME_FILE = BASE_DIR / "market_regime_report.json"

JSON_OUTPUT_FILE = BASE_DIR / "strategy_research_report.json"
TEXT_OUTPUT_FILE = BASE_DIR / "strategy_research_summary.txt"

SIGNAL_ORDER: Sequence[str] = (
    "NO TRADE",
    "WAIT",
    "WATCH",
    "SETUP",
    "HIGH PRIORITY",
)
ENGINE_NAMES: Sequence[str] = ("Trend", "Structure", "Momentum", "Risk")
NEAR_SETUP_SCORE = 23.0


def main() -> None:
    """Build strategy research artifacts and print a short summary."""
    report = build_strategy_research_report()
    save_json_report(report)
    save_text_summary(report)
    print_summary(report)


def build_strategy_research_report() -> Dict[str, Any]:
    """Build the full strategy research report."""
    diagnostics_rows = read_csv_rows(DIAGNOSTICS_FILE)
    debug_rows = read_csv_rows(DECISION_DEBUG_FILE)
    signal_rows = read_csv_rows(SIGNALS_FILE)
    trade_rows = read_csv_rows(TRADES_FILE)
    weights = read_json(WEIGHTS_FILE)
    regime_report = ensure_market_regime_report()

    report = {
        "generated_at": utc_now(),
        "source_files": {
            "decision_diagnostics": str(DIAGNOSTICS_FILE),
            "decision_debug": str(DECISION_DEBUG_FILE),
            "signals": str(SIGNALS_FILE),
            "trades": str(TRADES_FILE),
            "weights": str(WEIGHTS_FILE),
            "market_regime": str(MARKET_REGIME_FILE),
        },
        "weights_snapshot": weights,
        "decision_overview": build_decision_overview(signal_rows),
        "diagnostics_overview": build_diagnostics_overview(diagnostics_rows),
        "debug_overview": build_debug_overview(debug_rows),
        "trade_overview": build_trade_overview(signal_rows, trade_rows),
        "market_regime_overview": build_market_regime_overview(
            signal_rows,
            diagnostics_rows,
            trade_rows,
            regime_report,
        ),
    }
    report["recommendations"] = build_recommendations(report)
    return report


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read CSV rows while skipping empty rows and repeated headers."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))

    clean_rows: List[Dict[str, str]] = []
    for row in rows:
        if not row or not any(row.values()):
            continue
        timestamp = row.get("timestamp")
        if timestamp == "timestamp":
            continue
        clean_rows.append(row)
    return clean_rows


def read_json(path: Path) -> Dict[str, Any]:
    """Read a JSON object safely."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    return data if isinstance(data, dict) else {}


def ensure_market_regime_report() -> Dict[str, Any]:
    """Build the current market regime report for snapshot-based analytics."""
    report = build_market_regime_report()
    with MARKET_REGIME_FILE.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)
    return report


def build_decision_overview(signal_rows: Sequence[Mapping[str, str]]) -> Dict[str, Any]:
    """Summarize signal frequency and symbol-level decision distributions."""
    decisions_by_symbol: Dict[str, Counter[str]] = defaultdict(Counter)
    direction_by_symbol: Dict[str, Counter[str]] = defaultdict(Counter)

    for row in signal_rows:
        symbol = row.get("symbol", "UNKNOWN")
        signal = row.get("signal", "UNKNOWN")
        direction = row.get("direction", "UNKNOWN")
        decisions_by_symbol[symbol][signal] += 1
        direction_by_symbol[symbol][direction] += 1

    total_signal_counts = Counter()
    for counter in decisions_by_symbol.values():
        total_signal_counts.update(counter)

    per_symbol = {}
    for symbol in sorted(decisions_by_symbol):
        counts = decisions_by_symbol[symbol]
        total = sum(counts.values())
        per_symbol[symbol] = {
            "total_decisions": total,
            "signals": {name: counts.get(name, 0) for name in SIGNAL_ORDER},
            "long_short_bias": dict(direction_by_symbol[symbol]),
        }

    return {
        "total_rows": len(signal_rows),
        "signal_counts": {name: total_signal_counts.get(name, 0) for name in SIGNAL_ORDER},
        "per_symbol": per_symbol,
    }


def build_diagnostics_overview(
    diagnostics_rows: Sequence[Mapping[str, str]],
) -> Dict[str, Any]:
    """Summarize blockers, lost score, and near-setup opportunities."""
    primary_blockers = Counter()
    lost_scores_by_symbol: Dict[str, List[float]] = defaultdict(list)
    filter_failures_by_symbol: Dict[str, Counter[str]] = defaultdict(Counter)
    potential_by_symbol: Dict[str, List[float]] = defaultdict(list)

    for row in diagnostics_rows:
        symbol = row.get("symbol", "UNKNOWN")
        blocker = row.get("primary_blocker", "")
        if blocker:
            primary_blockers[blocker] += 1

        lost_score = safe_float(row.get("lost_score"))
        if lost_score is not None:
            lost_scores_by_symbol[symbol].append(lost_score)

        potential_score = safe_float(row.get("potential_score"))
        if potential_score is not None:
            potential_by_symbol[symbol].append(potential_score)

        for engine in ("trend", "structure", "momentum", "risk"):
            if row.get(engine, "").upper() == "FAIL":
                filter_failures_by_symbol[symbol][engine.title()] += 1

    near_setup = {}
    for symbol, scores in potential_by_symbol.items():
        qualifying = [score for score in scores if score >= NEAR_SETUP_SCORE]
        if not qualifying:
            continue
        near_setup[symbol] = {
            "near_setup_count": len(qualifying),
            "average_potential_score": round(mean(qualifying), 2),
            "max_potential_score": round(max(qualifying), 2),
        }

    weak_symbols = {}
    for symbol, failures in filter_failures_by_symbol.items():
        total_failures = sum(failures.values())
        if total_failures == 0:
            continue
        dominant = max(failures, key=failures.get)
        if failures[dominant] / total_failures >= 0.5:
            weak_symbols[symbol] = {
                "dominant_weak_filter": dominant,
                "failures": dict(failures),
            }

    return {
        "total_rows": len(diagnostics_rows),
        "primary_blockers": dict(primary_blockers),
        "average_lost_score": round(
            mean(flatten(lost_scores_by_symbol.values())),
            2,
        ) if lost_scores_by_symbol else 0,
        "average_lost_score_by_symbol": {
            symbol: round(mean(scores), 2)
            for symbol, scores in sorted(lost_scores_by_symbol.items())
            if scores
        },
        "near_setup_symbols": dict(
            sorted(
                near_setup.items(),
                key=lambda item: (
                    item[1]["near_setup_count"],
                    item[1]["average_potential_score"],
                ),
                reverse=True,
            )
        ),
        "weak_filter_symbols": weak_symbols,
    }


def build_debug_overview(debug_rows: Sequence[Mapping[str, str]]) -> Dict[str, Any]:
    """Summarize engine contribution gaps and LONG/SHORT bias."""
    loss_by_engine = Counter()
    direction_counts = Counter()
    symbol_bias = defaultdict(Counter)

    for row in debug_rows:
        symbol = row.get("symbol", "UNKNOWN")
        direction = row.get("direction", "UNKNOWN")
        direction_counts[direction] += 1
        symbol_bias[symbol][direction] += 1

        for engine in ("trend", "structure", "momentum", "risk"):
            long_key = f"{engine}_long"
            short_key = f"{engine}_short"
            long_value = safe_float(row.get(long_key), 0.0)
            short_value = safe_float(row.get(short_key), 0.0)
            if long_value is None or short_value is None:
                continue
            loss_by_engine[engine.title()] += abs(long_value - short_value)

    return {
        "total_rows": len(debug_rows),
        "long_short_bias": dict(direction_counts),
        "long_short_bias_by_symbol": {
            symbol: dict(counter)
            for symbol, counter in sorted(symbol_bias.items())
        },
        "lost_contribution_by_engine": {
            engine: round(value, 2)
            for engine, value in loss_by_engine.items()
        },
    }


def build_trade_overview(
    signal_rows: Sequence[Mapping[str, str]],
    trade_rows: Sequence[Mapping[str, str]],
) -> Dict[str, Any]:
    """Summarize trade performance when closed trades exist."""
    closed = [row for row in trade_rows if row.get("status") in {"WIN", "LOSS"}]
    if not closed:
        return {
            "closed_trades": 0,
            "winrate": 0,
            "average_pnl": 0,
            "by_symbol": {},
            "by_direction": {},
            "signal_to_outcome": {},
        }

    wins = [row for row in closed if row.get("status") == "WIN"]
    by_symbol: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"wins": 0, "losses": 0, "pnl": []}
    )
    by_direction: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"wins": 0, "losses": 0, "pnl": []}
    )

    signal_lookup = build_signal_lookup(signal_rows)
    signal_to_outcome: Dict[str, Counter[str]] = defaultdict(Counter)

    for row in closed:
        symbol = row.get("symbol", "UNKNOWN")
        direction = row.get("direction", "UNKNOWN")
        status = row.get("status", "UNKNOWN")
        pnl = safe_float(row.get("pnl"), 0.0) or 0.0

        bucket = by_symbol[symbol]
        bucket["wins" if status == "WIN" else "losses"] += 1
        bucket["pnl"].append(pnl)

        direction_bucket = by_direction[direction]
        direction_bucket["wins" if status == "WIN" else "losses"] += 1
        direction_bucket["pnl"].append(pnl)

        signal_name = signal_lookup.get((symbol, direction), "UNKNOWN")
        signal_to_outcome[signal_name][status] += 1

    return {
        "closed_trades": len(closed),
        "winrate": round(len(wins) / len(closed) * 100, 2),
        "average_pnl": round(mean([safe_float(row.get("pnl"), 0.0) or 0.0 for row in closed]), 2),
        "by_symbol": {
            symbol: summarize_trade_bucket(bucket)
            for symbol, bucket in sorted(by_symbol.items())
        },
        "by_direction": {
            direction: summarize_trade_bucket(bucket)
            for direction, bucket in sorted(by_direction.items())
        },
        "signal_to_outcome": {
            signal: dict(counter)
            for signal, counter in sorted(signal_to_outcome.items())
        },
    }


def build_market_regime_overview(
    signal_rows: Sequence[Mapping[str, str]],
    diagnostics_rows: Sequence[Mapping[str, str]],
    trade_rows: Sequence[Mapping[str, str]],
    regime_report: Mapping[str, Any],
) -> Dict[str, Any]:
    """Summarize performance and blockers by the latest known regime per symbol.

    This is snapshot-based analytics because legacy logs do not store EMA/ATR/RSI/MACD
    for every historical decision row.
    """
    symbols_payload = regime_report.get("symbols", {})
    symbol_to_regime = {
        symbol: payload.get("primary_regime", "UNKNOWN")
        for symbol, payload in symbols_payload.items()
    }
    if not symbol_to_regime:
        return {
            "mode": "snapshot_based",
            "by_regime": {},
            "notes": regime_report.get("notes", []),
        }

    closed = [row for row in trade_rows if row.get("status") in {"WIN", "LOSS"}]
    wins_by_regime: Dict[str, int] = Counter()
    totals_by_regime: Dict[str, int] = Counter()
    blockers_by_regime: Dict[str, Counter[str]] = defaultdict(Counter)
    good_setups_by_regime: Dict[str, int] = Counter()

    pnl_by_regime: Dict[str, List[float]] = defaultdict(list)

    for row in closed:
        regime = symbol_to_regime.get(row.get("symbol", ""), "UNKNOWN")
        totals_by_regime[regime] += 1
        if row.get("status") == "WIN":
            wins_by_regime[regime] += 1
        pnl_by_regime[regime].append(safe_float(row.get("pnl"), 0.0) or 0.0)

    for row in diagnostics_rows:
        regime = symbol_to_regime.get(row.get("symbol", ""), "UNKNOWN")
        blocker = row.get("primary_blocker", "UNKNOWN")
        blockers_by_regime[regime][blocker] += 1

    for row in signal_rows:
        if row.get("signal") not in {"SETUP", "HIGH PRIORITY"}:
            continue
        regime = symbol_to_regime.get(row.get("symbol", ""), "UNKNOWN")
        good_setups_by_regime[regime] += 1

    by_regime = {}
    for regime in sorted(set(symbol_to_regime.values()) | set(blockers_by_regime.keys())):
        total = totals_by_regime.get(regime, 0)
        by_regime[regime] = {
            "winrate": round(wins_by_regime.get(regime, 0) / total * 100, 2) if total else 0,
            "closed_trades": total,
            "average_pnl": round(mean(pnl_by_regime.get(regime, [0.0])), 2) if pnl_by_regime.get(regime) else 0,
            "primary_blockers": dict(blockers_by_regime.get(regime, {})),
            "good_setups": good_setups_by_regime.get(regime, 0),
            "symbols": [
                symbol for symbol, symbol_regime in symbol_to_regime.items()
                if symbol_regime == regime
            ],
        }

    return {
        "mode": "snapshot_based",
        "by_regime": by_regime,
        "notes": regime_report.get("notes", []),
    }


def build_signal_lookup(
    signal_rows: Sequence[Mapping[str, str]],
) -> Dict[tuple[str, str], str]:
    """Map symbol and direction to the latest signal label."""
    latest: Dict[tuple[str, str], tuple[str, str]] = {}
    for row in signal_rows:
        key = (row.get("symbol", "UNKNOWN"), row.get("direction", "UNKNOWN"))
        timestamp = row.get("timestamp", "")
        signal = row.get("signal", "UNKNOWN")
        if key not in latest or timestamp > latest[key][0]:
            latest[key] = (timestamp, signal)
    return {key: value[1] for key, value in latest.items()}


def summarize_trade_bucket(bucket: Mapping[str, Any]) -> Dict[str, Any]:
    """Summarize wins, losses, and average pnl for a trade bucket."""
    pnl_values = list(bucket.get("pnl", []))
    total = bucket.get("wins", 0) + bucket.get("losses", 0)
    return {
        "wins": bucket.get("wins", 0),
        "losses": bucket.get("losses", 0),
        "winrate": round(bucket.get("wins", 0) / total * 100, 2) if total else 0,
        "average_pnl": round(mean(pnl_values), 2) if pnl_values else 0,
    }


def build_recommendations(report: Mapping[str, Any]) -> List[str]:
    """Generate research recommendations without applying changes."""
    recommendations = []

    diagnostics = report.get("diagnostics_overview", {})
    debug = report.get("debug_overview", {})
    trades = report.get("trade_overview", {})
    market_regime = report.get("market_regime_overview", {})

    primary_blockers = diagnostics.get("primary_blockers", {})
    if primary_blockers:
        top_blocker = max(primary_blockers, key=primary_blockers.get)
        recommendations.append(
            f"Check whether {top_blocker} is too strict; it is the most frequent blocker."
        )

    lost_contribution = debug.get("lost_contribution_by_engine", {})
    if lost_contribution:
        top_loss_engine = max(lost_contribution, key=lost_contribution.get)
        recommendations.append(
            f"Review {top_loss_engine} contribution gaps first; it produces the largest lost contribution."
        )

    weak_symbols = diagnostics.get("weak_filter_symbols", {})
    for symbol, data in sorted(weak_symbols.items()):
        dominant = data.get("dominant_weak_filter")
        recommendations.append(
            f"{symbol} repeatedly shows weak {dominant}; verify whether this symbol belongs in the active universe."
        )

    near_setup = diagnostics.get("near_setup_symbols", {})
    if near_setup:
        top_symbols = ", ".join(list(near_setup.keys())[:3])
        recommendations.append(
            f"Keep {top_symbols} in backtest research first; they most often approach SETUP."
        )

    long_short_bias = debug.get("long_short_bias", {})
    long_count = long_short_bias.get("LONG", 0)
    short_count = long_short_bias.get("SHORT", 0)
    if long_count and short_count:
        bias_ratio = max(long_count, short_count) / min(long_count, short_count)
        if bias_ratio >= 1.5:
            dominant = "LONG" if long_count > short_count else "SHORT"
            recommendations.append(
                f"There is a noticeable {dominant} bias; test market-regime sensitivity in backtest."
            )

    if trades.get("closed_trades", 0):
        winrate = trades.get("winrate", 0)
        if winrate < 40:
            recommendations.append(
                "Closed trade performance is weak; test stricter entry selection in backtest before changing live weights."
            )
        recommendations.append(
            "Consider moving backtest research knobs into config only after the current weak filters are confirmed statistically."
        )
    else:
        recommendations.append(
            "There are no closed trades yet; collect more trade outcomes before recommending live strategy adjustments."
        )

    regime_stats = market_regime.get("by_regime", {})
    if regime_stats:
        best_regime = max(
            regime_stats,
            key=lambda key: (
                regime_stats[key].get("winrate", 0),
                regime_stats[key].get("good_setups", 0),
            ),
        )
        recommendations.append(
            f"Current snapshot suggests the strongest environment is {best_regime}; compare backtest ideas against that regime first."
        )

    if not recommendations:
        recommendations.append(
            "No strong research signal detected yet; gather more diagnostics before testing strategy changes."
        )

    return recommendations


def save_json_report(report: Mapping[str, Any]) -> None:
    """Write the JSON research report."""
    with JSON_OUTPUT_FILE.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)


def save_text_summary(report: Mapping[str, Any]) -> None:
    """Write a readable text summary."""
    decision_overview = report.get("decision_overview", {})
    diagnostics = report.get("diagnostics_overview", {})
    debug = report.get("debug_overview", {})
    trades = report.get("trade_overview", {})

    lines = [
        "AITradingAgent Strategy Research",
        f"Generated at: {report.get('generated_at', '')}",
        "",
        "Decision overview",
        f"Total decisions: {decision_overview.get('total_rows', 0)}",
    ]

    signal_counts = decision_overview.get("signal_counts", {})
    for signal_name in SIGNAL_ORDER:
        lines.append(f"{signal_name}: {signal_counts.get(signal_name, 0)}")

    lines.extend(
        [
            "",
            "Diagnostics overview",
            f"Total diagnostics: {diagnostics.get('total_rows', 0)}",
            f"Average lost score: {diagnostics.get('average_lost_score', 0)}",
            "Primary blockers:",
        ]
    )
    for blocker, count in diagnostics.get("primary_blockers", {}).items():
        lines.append(f"- {blocker}: {count}")

    lines.extend(
        [
            "",
            "Near setup symbols",
        ]
    )
    near_setup = diagnostics.get("near_setup_symbols", {})
    if near_setup:
        for symbol, data in near_setup.items():
            lines.append(
                f"- {symbol}: near={data['near_setup_count']}, "
                f"avg_potential={data['average_potential_score']}, "
                f"max_potential={data['max_potential_score']}"
            )
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            "Lost contribution by engine",
        ]
    )
    for engine, value in debug.get("lost_contribution_by_engine", {}).items():
        lines.append(f"- {engine}: {value}")

    lines.extend(
        [
            "",
            "Market regime overview",
        ]
    )
    market_regime = report.get("market_regime_overview", {})
    for regime, data in market_regime.get("by_regime", {}).items():
        lines.append(
            f"- {regime}: winrate={data.get('winrate', 0)}%, "
            f"good_setups={data.get('good_setups', 0)}, "
            f"closed_trades={data.get('closed_trades', 0)}"
        )
        blockers = data.get("primary_blockers", {})
        if blockers:
            lines.append(f"  blockers={blockers}")
    if market_regime.get("notes"):
        lines.append("Notes:")
        for note in market_regime["notes"]:
            lines.append(f"- {note}")

    lines.extend(
        [
            "",
            "Trade overview",
            f"Closed trades: {trades.get('closed_trades', 0)}",
            f"Winrate: {trades.get('winrate', 0)}%",
            f"Average PnL: {trades.get('average_pnl', 0)}",
            "",
            "Recommendations",
        ]
    )
    for item in report.get("recommendations", []):
        lines.append(f"- {item}")

    with TEXT_OUTPUT_FILE.open("w", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")


def print_summary(report: Mapping[str, Any]) -> None:
    """Print a compact summary in the terminal."""
    print("Strategy Research")
    print(f"Decisions analyzed : {report['decision_overview']['total_rows']}")
    print(f"Diagnostics rows   : {report['diagnostics_overview']['total_rows']}")
    print(f"Closed trades      : {report['trade_overview']['closed_trades']}")
    print(f"JSON report        : {JSON_OUTPUT_FILE}")
    print(f"Text summary       : {TEXT_OUTPUT_FILE}")


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Convert values to float safely."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def flatten(values: Iterable[Iterable[float]]) -> List[float]:
    """Flatten nested iterables of floats."""
    items: List[float] = []
    for group in values:
        items.extend(group)
    return items


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    main()
