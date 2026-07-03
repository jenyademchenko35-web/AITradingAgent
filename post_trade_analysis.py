"""Post-trade analysis for closed AITradingAgent trades."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


BASE_DIR = Path(__file__).resolve().parent
TRADES_FILE = BASE_DIR / "trades.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
REGIME_FILE = BASE_DIR / "market_regime_report.json"

JSON_OUTPUT = BASE_DIR / "post_trade_analysis_report.json"
TEXT_OUTPUT = BASE_DIR / "post_trade_analysis_summary.txt"
CSV_OUTPUT = BASE_DIR / "post_trade_analysis_trades.csv"

FILTERS = ("Trend", "Structure", "Momentum", "Risk")


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


def parse_time(value: Any) -> Optional[datetime]:
    """Parse ISO timestamps and normalize them to UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read CSV rows, ignoring empty rows and repeated headers."""
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


def group_by_symbol(rows: Iterable[Mapping[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    """Group rows by symbol and sort them by timestamp."""
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
    time_field: str = "timestamp",
) -> Optional[Dict[str, str]]:
    """Find the nearest row at or before target_time."""
    if target_time is None:
        return None
    best_row: Optional[Dict[str, str]] = None
    best_time: Optional[datetime] = None
    for row in rows:
        row_time = parse_time(row.get(time_field))
        if row_time is None or row_time > target_time:
            continue
        if best_time is None or row_time > best_time:
            best_time = row_time
            best_row = dict(row)
    return best_row


def row_age_minutes(
    row: Optional[Mapping[str, str]],
    target_time: Optional[datetime],
) -> Optional[float]:
    """Return row age in minutes relative to target_time."""
    if row is None or target_time is None:
        return None
    row_time = parse_time(row.get("timestamp"))
    if row_time is None:
        return None
    return round((target_time - row_time).total_seconds() / 60, 2)


def contribution(debug_row: Mapping[str, str], direction: str, engine: str) -> float:
    """Return engine contribution for the trade direction."""
    key = f"{engine.lower()}_{direction.lower()}"
    return safe_float(debug_row.get(key))


def pass_fail_from_debug(
    debug_row: Optional[Mapping[str, str]],
    direction: str,
) -> Tuple[List[str], List[str]]:
    """Derive PASS/FAIL from direction-specific engine contributions."""
    if debug_row is None or direction not in {"LONG", "SHORT"}:
        return [], []
    passed = []
    failed = []
    for engine in FILTERS:
        value = contribution(debug_row, direction, engine)
        if value > 0:
            passed.append(engine)
        else:
            failed.append(engine)
    return passed, failed


def pass_fail_from_diagnostics(
    diagnostic_row: Optional[Mapping[str, str]],
) -> Tuple[List[str], List[str]]:
    """Read PASS/FAIL lists from diagnostics when available."""
    if diagnostic_row is None:
        return [], []
    passed = []
    failed = []
    mapping = {
        "Trend": "trend",
        "Structure": "structure",
        "Momentum": "momentum",
        "Risk": "risk",
    }
    for engine, column in mapping.items():
        status = str(diagnostic_row.get(column, "")).upper()
        if status == "PASS":
            passed.append(engine)
        elif status == "FAIL":
            failed.append(engine)
    return passed, failed


def market_regime_for_symbol(symbol: str, report: Mapping[str, Any]) -> Dict[str, str]:
    """Return the current market-regime snapshot for a symbol."""
    payload = report.get("symbols", {}).get(symbol, {})
    if not isinstance(payload, dict):
        return {}
    return {
        "primary_regime": str(payload.get("primary_regime", "N/A")),
        "volatility_regime": str(payload.get("volatility_regime", "N/A")),
        "momentum_state": str(payload.get("momentum_state", "N/A")),
    }


def loss_reasons(
    trade: Mapping[str, Any],
    debug_row: Optional[Mapping[str, str]],
    failed: Iterable[str],
    confidence: float,
    signal_age: Optional[float],
    regime: Mapping[str, str],
) -> List[str]:
    """Infer possible reasons for a losing trade."""
    reasons = []
    failed_set = set(failed)
    if "Momentum" in failed_set:
        reasons.append("weak Momentum")
    if "Structure" in failed_set:
        reasons.append("weak Structure")
    if "Risk" in failed_set:
        reasons.append("bad Risk")
    if confidence and confidence < 70:
        reasons.append("low Confidence")
    if signal_age is not None and signal_age > 30:
        reasons.append("late Entry")
    if "High" in regime.get("volatility_regime", ""):
        reasons.append("high Volatility")
    if "Trend" in failed_set:
        reasons.append("counter-trend trade")

    direction = trade.get("direction", "")
    if debug_row is not None and direction in {"LONG", "SHORT"}:
        trend_value = contribution(debug_row, direction, "Trend")
        if trend_value <= 0 and "counter-trend trade" not in reasons:
            reasons.append("counter-trend trade")
    return reasons or ["unclassified LOSS"]


def win_factors(
    debug_row: Optional[Mapping[str, str]],
    passed: Iterable[str],
    confidence: float,
) -> List[str]:
    """Infer factors that likely helped a winning trade."""
    factors = []
    passed_set = set(passed)
    if "Trend" in passed_set:
        factors.append("strong Trend")
    if "Structure" in passed_set:
        factors.append("good Structure")
    if "Risk" in passed_set:
        factors.append("good Risk")
    if "Momentum" in passed_set:
        factors.append("strong Momentum")
    if confidence >= 75:
        factors.append("high Confidence")
    if debug_row is None and not factors:
        factors.append("unclassified WIN")
    return factors or ["unclassified WIN"]


def summarize_group(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    """Summarize analyzed trades for a symbol or direction."""
    wins = sum(1 for row in rows if row.get("result") == "WIN")
    losses = sum(1 for row in rows if row.get("result") == "LOSS")
    pnl = sum(safe_float(row.get("pnl")) for row in rows)
    total = len(rows)
    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "winrate": round((wins / total * 100) if total else 0.0, 2),
        "pnl": round(pnl, 2),
    }


def analyze_trades() -> Dict[str, Any]:
    """Build the post-trade analysis report."""
    trades = [
        row for row in read_csv_rows(TRADES_FILE)
        if row.get("result") in {"WIN", "LOSS"} or row.get("status") in {"WIN", "LOSS"}
    ]
    signals_by_symbol = group_by_symbol(read_csv_rows(SIGNALS_FILE))
    debug_by_symbol = group_by_symbol(read_csv_rows(DEBUG_FILE))
    diagnostics_by_symbol = group_by_symbol(read_csv_rows(DIAGNOSTICS_FILE))
    regime_report = read_json(REGIME_FILE)

    analyzed: List[Dict[str, Any]] = []
    loss_counter: Counter[str] = Counter()
    win_counter: Counter[str] = Counter()

    for trade in trades:
        symbol = trade.get("symbol", "")
        opened_at = parse_time(trade.get("opened_at"))
        direction = trade.get("direction", "")
        result = trade.get("result") or trade.get("status", "")

        signal_row = nearest_before(signals_by_symbol.get(symbol, []), opened_at)
        debug_row = nearest_before(debug_by_symbol.get(symbol, []), opened_at)
        diagnostic_row = nearest_before(diagnostics_by_symbol.get(symbol, []), opened_at)

        passed, failed = pass_fail_from_diagnostics(diagnostic_row)
        if not passed and not failed:
            passed, failed = pass_fail_from_debug(debug_row, direction)

        score = safe_float(
            (debug_row or signal_row or {}).get("score"),
        )
        confidence = safe_float(
            (debug_row or signal_row or {}).get("confidence"),
        )
        primary_blocker = (
            diagnostic_row.get("primary_blocker", "")
            if diagnostic_row else ""
        ) or (failed[0] if failed else "N/A")
        regime = market_regime_for_symbol(symbol, regime_report)
        signal_age = row_age_minutes(signal_row or debug_row, opened_at)

        if result == "LOSS":
            reasons = loss_reasons(
                trade,
                debug_row,
                failed,
                confidence,
                signal_age,
                regime,
            )
            loss_counter.update(reasons)
            factors = []
        else:
            reasons = []
            factors = win_factors(debug_row, passed, confidence)
            win_counter.update(factors)

        analyzed.append(
            {
                "symbol": symbol,
                "direction": direction,
                "result": result,
                "pnl": safe_float(trade.get("pnl")),
                "entry": safe_float(trade.get("entry")),
                "stop_loss": safe_float(trade.get("stop_loss")),
                "take_profit": safe_float(trade.get("take_profit")),
                "opened_at": trade.get("opened_at", ""),
                "closed_at": trade.get("closed_at", ""),
                "matched_signal_time": (signal_row or {}).get("timestamp", ""),
                "matched_debug_time": (debug_row or {}).get("timestamp", ""),
                "signal_age_minutes": signal_age,
                "score": score,
                "confidence": confidence,
                "primary_blocker": primary_blocker,
                "passed_filters": passed,
                "failed_filters": failed,
                "market_regime": regime.get("primary_regime", "N/A"),
                "volatility_regime": regime.get("volatility_regime", "N/A"),
                "loss_reasons": reasons,
                "win_factors": factors,
            }
        )

    wins = [row for row in analyzed if row["result"] == "WIN"]
    losses = [row for row in analyzed if row["result"] == "LOSS"]
    by_symbol = {
        symbol: summarize_group([row for row in analyzed if row["symbol"] == symbol])
        for symbol in sorted({row["symbol"] for row in analyzed})
    }
    by_direction = {
        direction: summarize_group([row for row in analyzed if row["direction"] == direction])
        for direction in sorted({row["direction"] for row in analyzed})
    }

    best_symbol = "N/A"
    worst_symbol = "N/A"
    if by_symbol:
        best_symbol = max(
            by_symbol,
            key=lambda key: (by_symbol[key]["pnl"], by_symbol[key]["winrate"]),
        )
        worst_symbol = min(
            by_symbol,
            key=lambda key: (by_symbol[key]["pnl"], by_symbol[key]["winrate"]),
        )

    top_loss = loss_counter.most_common(1)
    recommendation = (
        f"Сначала улучшать: {top_loss[0][0]}."
        if top_loss else
        "Недостаточно закрытых LOSS-сделок для рекомендации."
    )

    return {
        "generated_at": utc_now(),
        "status": "OK",
        "closed_trades": len(analyzed),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round((len(wins) / len(analyzed) * 100) if analyzed else 0.0, 2),
        "top_loss_reasons": dict(loss_counter.most_common()),
        "top_win_factors": dict(win_counter.most_common()),
        "by_symbol": by_symbol,
        "by_direction": by_direction,
        "best_symbol": best_symbol,
        "worst_symbol": worst_symbol,
        "recommendation": recommendation,
        "trades": analyzed,
    }


def save_csv(trades: List[Mapping[str, Any]]) -> None:
    """Save per-trade analysis to CSV."""
    columns = [
        "symbol",
        "direction",
        "result",
        "pnl",
        "entry",
        "stop_loss",
        "take_profit",
        "opened_at",
        "closed_at",
        "matched_signal_time",
        "matched_debug_time",
        "signal_age_minutes",
        "score",
        "confidence",
        "primary_blocker",
        "passed_filters",
        "failed_filters",
        "market_regime",
        "volatility_regime",
        "loss_reasons",
        "win_factors",
    ]
    with CSV_OUTPUT.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        for trade in trades:
            row = dict(trade)
            row["passed_filters"] = "|".join(row.get("passed_filters", []))
            row["failed_filters"] = "|".join(row.get("failed_filters", []))
            row["loss_reasons"] = "|".join(row.get("loss_reasons", []))
            row["win_factors"] = "|".join(row.get("win_factors", []))
            writer.writerow({column: row.get(column, "") for column in columns})


def save_summary(report: Mapping[str, Any]) -> None:
    """Save a compact text summary."""
    lines = [
        "AITradingAgent Post Trade Analysis",
        f"Generated at: {report.get('generated_at')}",
        f"Closed trades: {report.get('closed_trades', 0)}",
        f"Wins: {report.get('wins', 0)}",
        f"Losses: {report.get('losses', 0)}",
        f"Winrate: {report.get('winrate', 0)}%",
        "",
        "Top LOSS reasons:",
    ]
    for reason, count in report.get("top_loss_reasons", {}).items():
        lines.append(f"- {reason}: {count}")
    lines.append("")
    lines.append("Top WIN factors:")
    for factor, count in report.get("top_win_factors", {}).items():
        lines.append(f"- {factor}: {count}")
    lines.extend(
        [
            "",
            f"Best symbol: {report.get('best_symbol', 'N/A')}",
            f"Worst symbol: {report.get('worst_symbol', 'N/A')}",
            f"Recommendation: {report.get('recommendation', 'N/A')}",
        ]
    )
    TEXT_OUTPUT.write_text("\n".join(lines), encoding="utf-8")


def save_report(report: Dict[str, Any]) -> None:
    """Save all post-trade artifacts."""
    with JSON_OUTPUT.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)
    save_csv(report.get("trades", []))
    save_summary(report)


def print_summary(report: Mapping[str, Any]) -> None:
    """Print a compact CLI summary."""
    print("Post Trade Analysis")
    print(f"Closed trades : {report.get('closed_trades', 0)}")
    print(f"Wins          : {report.get('wins', 0)}")
    print(f"Losses        : {report.get('losses', 0)}")
    print(f"Winrate       : {report.get('winrate', 0)}%")
    print(f"Best symbol   : {report.get('best_symbol', 'N/A')}")
    print(f"Worst symbol  : {report.get('worst_symbol', 'N/A')}")
    print(f"Recommendation: {report.get('recommendation', 'N/A')}")


def main() -> None:
    """Build post-trade analysis artifacts."""
    report = analyze_trades()
    save_report(report)
    print_summary(report)


if __name__ == "__main__":
    main()
