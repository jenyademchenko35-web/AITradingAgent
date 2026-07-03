"""AI Coach advisory layer for AITradingAgent.

This module reads existing analytics artifacts and turns them into a compact
human-readable daily recommendation. It does not change trading logic, weights,
or DecisionEngine behavior.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional


BASE_DIR = Path(__file__).resolve().parent

STATS_FILE = BASE_DIR / "agent_v3_stats.json"
TRADES_FILE = BASE_DIR / "trades.csv"
FILTERS_REPORT_FILE = BASE_DIR / "filter_effectiveness_report.json"
POST_TRADE_REPORT_FILE = BASE_DIR / "post_trade_analysis_report.json"
MARKET_REGIME_FILE = BASE_DIR / "market_regime_report.json"
AUTO_LEARNING_FILE = BASE_DIR / "auto_learning_recommendation.json"
EXPERIMENTS_REPORT_FILE = BASE_DIR / "strategy_experiments_report.json"
DATA_QUALITY_FILE = BASE_DIR / "data_quality_report.json"
BLOCKED_ALL_FILE = BASE_DIR / "blocked_trade_simulation_ALL_report.json"

JSON_OUTPUT = BASE_DIR / "ai_coach_report.json"
TEXT_OUTPUT = BASE_DIR / "ai_coach_summary.txt"


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


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read CSV rows while ignoring empty rows."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    return [
        row for row in rows
        if row and any(row.values()) and row.get("timestamp") != "timestamp"
    ]


def closed_trades(limit: Optional[int] = None) -> List[Dict[str, str]]:
    """Return closed WIN/LOSS trades, newest first."""
    rows = [
        row for row in read_csv_rows(TRADES_FILE)
        if row.get("result") in {"WIN", "LOSS"}
        or row.get("status") in {"WIN", "LOSS"}
    ]
    rows.sort(key=lambda row: row.get("closed_at") or row.get("opened_at", ""), reverse=True)
    return rows[:limit] if limit else rows


def winrate(rows: Iterable[Mapping[str, str]]) -> float:
    """Calculate winrate for trade rows."""
    items = list(rows)
    wins = sum(
        1 for row in items
        if (row.get("result") or row.get("status")) == "WIN"
    )
    return round((wins / len(items) * 100) if items else 0.0, 2)


def unique_market_regime(regime_report: Mapping[str, Any]) -> str:
    """Return a compact current market-regime label."""
    regimes = []
    for payload in regime_report.get("symbols", {}).values():
        regime = payload.get("primary_regime")
        if regime and regime not in regimes:
            regimes.append(regime)
    if not regimes:
        return "N/A"
    if len(regimes) == 1:
        return regimes[0]
    return ", ".join(regimes[:3])


def top_key(payload: Mapping[str, Any]) -> str:
    """Return key with the highest numeric value from a mapping."""
    if not payload:
        return "N/A"
    return max(payload.items(), key=lambda item: safe_float(item[1]))[0]


def best_and_worst_symbol(trades: Iterable[Mapping[str, str]]) -> Dict[str, str]:
    """Find best and worst symbol by recent PnL."""
    pnl_by_symbol: Counter[str] = Counter()
    for row in trades:
        symbol = row.get("symbol", "N/A")
        pnl_by_symbol[symbol] += safe_float(row.get("pnl"))
    if not pnl_by_symbol:
        return {"best": "N/A", "worst": "N/A"}
    best = max(pnl_by_symbol, key=lambda key: pnl_by_symbol[key])
    worst = min(pnl_by_symbol, key=lambda key: pnl_by_symbol[key])
    return {"best": best, "worst": worst}


def build_actions(
    quality_status: str,
    closed_count: int,
    main_blocker: str,
    top_loss_reason: str,
    auto_status: str,
    blocked_report: Mapping[str, Any],
    symbols: Mapping[str, str],
) -> List[str]:
    """Build practical advisory actions from existing analytics."""
    actions: List[str] = []
    if quality_status not in {"OK", "N/A"}:
        actions.append("сначала исправить Data Quality")
    if closed_count < 30:
        actions.append(f"собрать ещё {30 - closed_count} закрытых сделок")
    if top_loss_reason == "bad Risk":
        actions.append("ждать только A-сетапы и внимательно проверять Risk")
    elif top_loss_reason == "low Confidence":
        actions.append("пропускать сделки с низкой Confidence")
    elif top_loss_reason == "weak Momentum":
        actions.append("не торговать против слабого Momentum")
    if main_blocker == "Momentum":
        actions.append("не ослаблять Momentum без новых blocked-trade доказательств")

    momentum = blocked_report.get("by_blocker", {}).get("Momentum", {})
    momentum_pf = safe_float(momentum.get("summary", {}).get("profit_factor"))
    if momentum_pf <= 1.0:
        actions.append("не открывать Momentum-blocked сделки вручную")

    if auto_status in {"NOT_ENOUGH_DATA", "REJECTED_BY_RULES", "BLOCKED"}:
        actions.append("не менять веса сегодня")

    best = symbols.get("best", "N/A")
    worst = symbols.get("worst", "N/A")
    if best != "N/A" and worst != "N/A" and best != worst:
        actions.append(f"сравнить {worst} против {best} перед новыми тестами")

    deduped = []
    for action in actions:
        if action not in deduped:
            deduped.append(action)
    return deduped[:5] or ["продолжать наблюдение без изменений стратегии"]


def build_report() -> Dict[str, Any]:
    """Build the AI Coach report."""
    stats = read_json(STATS_FILE)
    quality = read_json(DATA_QUALITY_FILE)
    filters = read_json(FILTERS_REPORT_FILE)
    post_trade = read_json(POST_TRADE_REPORT_FILE)
    regime = read_json(MARKET_REGIME_FILE)
    auto_learning = read_json(AUTO_LEARNING_FILE)
    experiments = read_json(EXPERIMENTS_REPORT_FILE)
    blocked_all = read_json(BLOCKED_ALL_FILE)

    recent_trades = closed_trades(limit=20)
    all_closed = closed_trades()
    symbols = best_and_worst_symbol(all_closed)

    ranking = filters.get("ranking", {})
    main_blocker = ranking.get("main_blocker", "N/A")
    top_loss_reason = top_key(post_trade.get("top_loss_reasons", {}))
    top_win_factor = top_key(post_trade.get("top_win_factors", {}))
    market = unique_market_regime(regime)
    best_experiment = experiments.get("best_scenario", {}).get("scenario", "N/A")

    closed_count = len(all_closed)
    quality_status = quality.get("status", "N/A")
    auto_status = auto_learning.get("status", "N/A")
    actions = build_actions(
        quality_status=quality_status,
        closed_count=closed_count,
        main_blocker=main_blocker,
        top_loss_reason=top_loss_reason,
        auto_status=auto_status,
        blocked_report=blocked_all,
        symbols=symbols,
    )

    return {
        "generated_at": utc_now(),
        "status": "OK",
        "agent_cycle": stats.get("runs", 0),
        "market_regime": market,
        "main_blocker": main_blocker,
        "closed_trades": closed_count,
        "recent_trades_window": len(recent_trades),
        "recent_winrate": winrate(recent_trades),
        "overall_winrate": post_trade.get("winrate", winrate(all_closed)),
        "top_loss_reason": top_loss_reason,
        "top_win_factor": top_win_factor,
        "best_symbol": symbols.get("best", "N/A"),
        "worst_symbol": symbols.get("worst", "N/A"),
        "data_quality": quality_status,
        "auto_learning": auto_status,
        "best_experiment": best_experiment,
        "actions": actions,
        "notes": [
            "AI Coach is advisory only.",
            "No trading logic, DecisionEngine, or weights are changed.",
        ],
    }


def format_summary(report: Mapping[str, Any]) -> str:
    """Format the AI Coach report for text/Telegram output."""
    actions = [f"- {item}" for item in report.get("actions", [])]
    return "\n".join(
        [
            "🧠 AI Coach",
            "",
            f"Сегодня рынок: {report.get('market_regime', 'N/A')}",
            f"Главный blocker: {report.get('main_blocker', 'N/A')}",
            (
                f"Последние {report.get('recent_trades_window', 0)} сделок: "
                f"WR {report.get('recent_winrate', 0)}%"
            ),
            f"Всего закрытых сделок: {report.get('closed_trades', 0)}",
            f"Главная проблема: {report.get('top_loss_reason', 'N/A')}",
            f"Что чаще работает: {report.get('top_win_factor', 'N/A')}",
            f"Data Quality: {report.get('data_quality', 'N/A')}",
            f"Auto-Learning: {report.get('auto_learning', 'N/A')}",
            f"Best Experiment: {report.get('best_experiment', 'N/A')}",
            f"Лучший символ: {report.get('best_symbol', 'N/A')}",
            f"Худший символ: {report.get('worst_symbol', 'N/A')}",
            "",
            "Что делать сегодня:",
            *actions,
        ]
    )


def save_report(report: Dict[str, Any]) -> None:
    """Save AI Coach artifacts."""
    with JSON_OUTPUT.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)
    TEXT_OUTPUT.write_text(format_summary(report), encoding="utf-8")


def main() -> None:
    """Build AI Coach artifacts."""
    report = build_report()
    save_report(report)
    print(format_summary(report))


if __name__ == "__main__":
    main()
