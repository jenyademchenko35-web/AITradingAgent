"""Read-only Adaptive Strategy Advisor for AITradingAgent.

The advisor aggregates existing runtime data, research reports and dry-run
logs to choose an observation mode. It never changes DecisionEngine, config,
weights, thresholds, live agent logic, or trade execution.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping

from best_candidate_ranker import rank_candidates


BASE_DIR = Path(__file__).resolve().parent

AGENT_STATS_FILE = BASE_DIR / "agent_v3_stats.json"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
TRADES_FILE = BASE_DIR / "trades.csv"
RESEARCH_HUB_FILE = BASE_DIR / "research_hub_report.json"
MARKET_REGIME_FILE = BASE_DIR / "market_regime_advisor_report.json"
RELAXED_EDGE_FILE = BASE_DIR / "relaxed_edge_dry_run.csv"
LONG_REBOUND_FILE = BASE_DIR / "long_rebound_opportunity_dry_run.csv"
ADA_DRY_RUN_FILE = BASE_DIR / "ada_opportunity_dry_run.csv"
DOGE_LINK_DRY_RUN_FILE = BASE_DIR / "doge_link_opportunity_dry_run.csv"
PIPELINE_FILE = BASE_DIR / "decision_pipeline_profile_report.json"
OPPORTUNITY_FILE = BASE_DIR / "trade_opportunity_expansion_report.json"

REPORT_FILE = BASE_DIR / "adaptive_strategy_advisor_report.json"
SUMMARY_FILE = BASE_DIR / "adaptive_strategy_advisor_summary.txt"
ACTIONS_FILE = BASE_DIR / "adaptive_strategy_advisor_actions.csv"

ADAPTIVE_MODES = {
    "NORMAL",
    "CONSERVATIVE",
    "RELAXED_OBSERVATION",
    "TREND_FOLLOWING_OBSERVATION",
    "RANGE_OBSERVATION",
    "INSUFFICIENT_DATA",
}


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    """Read JSON object safely."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read CSV rows safely."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with path.open("r", newline="", encoding="utf-8") as file:
            return [
                dict(row)
                for row in csv.DictReader(file)
                if row and any(row.values())
            ]
    except (csv.Error, OSError, UnicodeDecodeError):
        return []


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert values to float safely."""
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return default


def parse_time(value: str) -> datetime | None:
    """Parse ISO timestamp."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def today_rows(rows: Iterable[Mapping[str, str]]) -> list[Mapping[str, str]]:
    """Return rows from current UTC day."""
    today = datetime.now(timezone.utc).date()
    return [
        row for row in rows
        if (parse_time(str(row.get("timestamp", ""))) or datetime.min.replace(tzinfo=timezone.utc)).date() == today
    ]


def latest_by_symbol(rows: Iterable[Mapping[str, str]]) -> dict[str, dict[str, str]]:
    """Return latest signal row for each symbol."""
    latest: dict[str, dict[str, str]] = {}
    for row in rows:
        symbol = str(row.get("symbol", ""))
        if not symbol:
            continue
        if symbol not in latest or str(row.get("timestamp", "")) > latest[symbol].get("timestamp", ""):
            latest[symbol] = dict(row)
    return latest


def closed_trades(rows: Iterable[Mapping[str, str]]) -> list[Mapping[str, str]]:
    """Return closed trades only."""
    return [
        row for row in rows
        if (row.get("status") or row.get("result")) in {"WIN", "LOSS"}
    ]


def trade_stats(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Calculate compact trade statistics."""
    closed = closed_trades(rows)
    wins = [row for row in closed if (row.get("status") or row.get("result")) == "WIN"]
    losses = [row for row in closed if (row.get("status") or row.get("result")) == "LOSS"]
    pnls = [safe_float(row.get("pnl")) for row in closed]
    gross_profit = sum(max(pnl, 0.0) for pnl in pnls)
    gross_loss = sum(abs(min(pnl, 0.0)) for pnl in pnls)
    latest_close = latest_trade_time(closed)
    days_without_trades = 0
    if latest_close:
        days_without_trades = max(
            0,
            (datetime.now(timezone.utc) - latest_close).days,
        )
    return {
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round((len(wins) / len(closed) * 100) if closed else 0.0, 2),
        "profit_factor": round((gross_profit / gross_loss) if gross_loss else 0.0, 4),
        "net_pnl": round(sum(pnls), 2),
        "days_without_trades": days_without_trades,
        "latest_trade_time": latest_close.isoformat() if latest_close else None,
    }


def latest_trade_time(rows: Iterable[Mapping[str, str]]) -> datetime | None:
    """Return latest closed/opened trade timestamp."""
    times = []
    for row in rows:
        timestamp = parse_time(str(row.get("closed_at") or row.get("opened_at") or ""))
        if timestamp:
            times.append(timestamp)
    return max(times) if times else None


def dry_run_count(path: Path) -> int:
    """Count dry-run rows."""
    return len(read_csv_rows(path))


def avg(values: Iterable[float]) -> float:
    """Return average or zero."""
    items = [value for value in values if value is not None]
    return round(mean(items), 2) if items else 0.0


def direction_aligned_with_regime(direction: str, regime: str) -> bool:
    """Return whether a direction follows the current trend regime."""
    if regime == "TRENDING_UP":
        return direction == "LONG"
    if regime == "TRENDING_DOWN":
        return direction == "SHORT"
    return False


class AdaptiveStrategyAdvisor:
    """Read-only adaptive strategy advisor."""

    def __init__(self) -> None:
        self.agent_stats = read_json(AGENT_STATS_FILE)
        self.signals = read_csv_rows(SIGNALS_FILE)
        self.trades = read_csv_rows(TRADES_FILE)
        self.research_hub = read_json(RESEARCH_HUB_FILE)
        self.market_regime = read_json(MARKET_REGIME_FILE)
        self.pipeline = read_json(PIPELINE_FILE)
        self.opportunity = read_json(OPPORTUNITY_FILE)

    def build_snapshot(self) -> dict[str, Any]:
        """Build current read-only market and performance snapshot."""
        trade_metrics = trade_stats(self.trades)
        latest = latest_by_symbol(self.signals)
        ranked = rank_candidates(latest.values())
        near = [candidate for candidate in ranked if candidate.status == "NEAR SETUP"]
        best = ranked[0] if ranked else None
        today_signal_rows = today_rows(self.signals)
        dry_counts = {
            "relaxed_edge": dry_run_count(RELAXED_EDGE_FILE),
            "long_rebound": dry_run_count(LONG_REBOUND_FILE),
            "ada": dry_run_count(ADA_DRY_RUN_FILE),
            "doge_link": dry_run_count(DOGE_LINK_DRY_RUN_FILE),
        }
        market = (
            self.market_regime.get("current_market", {}).get("regime")
            or self.research_hub.get("overall_status", {}).get("current_market_regime")
            or "Недостаточно данных"
        )

        return {
            "days_without_trades": trade_metrics["days_without_trades"],
            "analyses_today": len(today_signal_rows),
            "near_setup_count": len(near),
            "relaxed_edge_dry_run_candidates": dry_counts["relaxed_edge"],
            "long_rebound_dry_run_candidates": dry_counts["long_rebound"],
            "ada_dry_run_candidates": dry_counts["ada"],
            "doge_link_dry_run_candidates": dry_counts["doge_link"],
            "total_dry_run_candidates": sum(dry_counts.values()),
            "best_symbol": best.symbol.replace("/USDT", "") if best else "N/A",
            "best_direction": best.direction if best else "N/A",
            "best_status": best.status if best else "N/A",
            "average_near_setup_edge": avg(candidate.edge for candidate in near),
            "average_confidence": avg(candidate.confidence for candidate in near),
            "market_regime": market,
            "closed_trades": trade_metrics["closed"],
            "wins": trade_metrics["wins"],
            "losses": trade_metrics["losses"],
            "winrate": trade_metrics["winrate"],
            "profit_factor": trade_metrics["profit_factor"],
            "net_pnl": trade_metrics["net_pnl"],
            "near_setup_symbols": [
                {
                    "symbol": candidate.symbol,
                    "direction": candidate.direction,
                    "confidence": candidate.confidence,
                    "edge": candidate.edge,
                    "weighted_score": candidate.weighted_score,
                    "trend_aligned": direction_aligned_with_regime(candidate.direction, market),
                }
                for candidate in near[:10]
            ],
        }

    def choose_mode(self, snapshot: Mapping[str, Any]) -> tuple[str, list[str]]:
        """Choose adaptive observation mode from existing data."""
        reasons: list[str] = []
        closed = safe_float(snapshot.get("closed_trades"))
        losses = safe_float(snapshot.get("losses"))
        pf = safe_float(snapshot.get("profit_factor"))
        near_count = int(snapshot.get("near_setup_count", 0))
        dry_total = int(snapshot.get("total_dry_run_candidates", 0))
        days_without = int(snapshot.get("days_without_trades", 0))
        regime = str(snapshot.get("market_regime", ""))
        long_rebound = int(snapshot.get("long_rebound_dry_run_candidates", 0))
        trend_aligned = any(
            item.get("trend_aligned")
            for item in snapshot.get("near_setup_symbols", [])
        )

        if closed < 10 and len(self.signals) < 500:
            reasons.append("Мало сделок и мало сигналов для адаптивного вывода.")
            return "INSUFFICIENT_DATA", reasons

        if losses > 0 and pf < 1:
            reasons.append("Profit Factor ниже 1 и убыточных сделок больше, чем прибыльных.")
            reasons.append("Приоритет: защита капитала, а не ослабление фильтров.")
            return "CONSERVATIVE", reasons

        if regime == "RANGING" and long_rebound > 0:
            reasons.append("Рынок RANGING и есть rebound/pullback Dry-run кандидаты.")
            return "RANGE_OBSERVATION", reasons

        if regime in {"TRENDING_UP", "TRENDING_DOWN"} and trend_aligned:
            reasons.append("Рынок трендовый и Near Setup совпадает с направлением тренда.")
            return "TREND_FOLLOWING_OBSERVATION", reasons

        if days_without >= 3 and near_count > 0 and dry_total > 0:
            reasons.append("Несколько дней нет сделок, но есть Near Setup и Dry-run кандидаты.")
            return "RELAXED_OBSERVATION", reasons

        reasons.append("Нет достаточных оснований менять режим наблюдения.")
        return "NORMAL", reasons

    def recommendations(self, mode: str, snapshot: Mapping[str, Any]) -> dict[str, list[str]]:
        """Build Russian recommendations and forbidden actions."""
        nearest = snapshot.get("near_setup_symbols", [])
        symbols = ", ".join(item.get("symbol", "").replace("/USDT", "") for item in nearest[:5])
        observe = []
        if symbols:
            observe.append(f"Наблюдать ближайшие Near Setup: {symbols}.")
        else:
            observe.append("Наблюдать появление новых Near Setup без изменения порогов.")

        if mode == "CONSERVATIVE":
            observe.append("Сохранять осторожный режим: PF ниже 1, live-логику не ослаблять.")
        elif mode == "RELAXED_OBSERVATION":
            observe.append("Собирать outcome для Relaxed Edge Dry-run кандидатов.")
        elif mode == "TREND_FOLLOWING_OBSERVATION":
            observe.append("Отдельно отслеживать Near Setup в сторону текущего тренда.")
        elif mode == "RANGE_OBSERVATION":
            observe.append("Отдельно отслеживать LONG rebound / pullback Dry-run.")
        elif mode == "INSUFFICIENT_DATA":
            observe.append("Сначала накопить больше сигналов и закрытых сделок.")

        can_relax = []
        if mode in {"RELAXED_OBSERVATION", "TREND_FOLLOWING_OBSERVATION", "RANGE_OBSERVATION"}:
            can_relax.append("Мягкий режим можно рассматривать только как Dry-run наблюдение.")
        else:
            can_relax.append("Мягкий режим пока не рассматривать для live.")

        forbidden = [
            "Не менять DecisionEngine.",
            "Не менять MIN_EDGE.",
            "Не менять MIN_SCORE.",
            "Не менять веса.",
            "Не открывать сделки по Dry-run сигналам.",
            "Не применять рекомендации автоматически.",
        ]
        why_not_live = [
            "Закрытых сделок меньше 30 или Profit Factor недостаточно надёжен.",
            "Dry-run кандидаты ещё не подтвердили качество outcome.",
            "Advisor mode предназначен только для наблюдения.",
        ]
        return {
            "what_to_observe": observe,
            "soft_mode": can_relax,
            "why_live_logic_must_not_change": why_not_live,
            "forbidden_actions": forbidden,
        }

    def build_actions(self, mode: str, snapshot: Mapping[str, Any]) -> list[dict[str, str]]:
        """Build action rows for CSV output."""
        actions = [
            {
                "priority": "1",
                "action": "COLLECT_MORE_DATA",
                "status": "ACTIVE",
                "reason": "Нужно больше закрытых сделок и outcome по Dry-run.",
            },
            {
                "priority": "2",
                "action": "OBSERVE_NEAR_SETUP",
                "status": "ACTIVE" if snapshot.get("near_setup_count", 0) else "WAITING",
                "reason": f"Лучший символ: {snapshot.get('best_symbol', 'N/A')}",
            },
            {
                "priority": "3",
                "action": "KEEP_LIVE_LOGIC_UNCHANGED",
                "status": "REQUIRED",
                "reason": f"Adaptive Mode: {mode}",
            },
        ]
        return actions

    def build_report(self) -> dict[str, Any]:
        """Build full advisor report."""
        snapshot = self.build_snapshot()
        mode, reasons = self.choose_mode(snapshot)
        recommendations = self.recommendations(mode, snapshot)
        actions = self.build_actions(mode, snapshot)
        return {
            "generated_at": utc_now(),
            "mode": "READ_ONLY",
            "adaptive_mode": mode,
            "mode_reasons": reasons,
            "snapshot": snapshot,
            "recommendations": recommendations,
            "actions": actions,
            "source_files": {
                "agent_v3_stats.json": file_state(AGENT_STATS_FILE),
                "signals_v3.csv": file_state(SIGNALS_FILE),
                "trades.csv": file_state(TRADES_FILE),
                "research_hub_report.json": file_state(RESEARCH_HUB_FILE),
                "market_regime_advisor_report.json": file_state(MARKET_REGIME_FILE),
                "relaxed_edge_dry_run.csv": file_state(RELAXED_EDGE_FILE),
                "long_rebound_opportunity_dry_run.csv": file_state(LONG_REBOUND_FILE),
                "ada_opportunity_dry_run.csv": file_state(ADA_DRY_RUN_FILE),
                "doge_link_opportunity_dry_run.csv": file_state(DOGE_LINK_DRY_RUN_FILE),
                "decision_pipeline_profile_report.json": file_state(PIPELINE_FILE),
                "trade_opportunity_expansion_report.json": file_state(OPPORTUNITY_FILE),
            },
            "apply_automatically": False,
            "notes": [
                "Advisor mode only: no live strategy changes.",
                "All recommendations require manual review and additional outcome data.",
            ],
        }

    def save_outputs(self, report: Mapping[str, Any]) -> None:
        """Save JSON, TXT and CSV artifacts."""
        with REPORT_FILE.open("w", encoding="utf-8") as file:
            json.dump(report, file, indent=2, ensure_ascii=False)
        SUMMARY_FILE.write_text(format_summary(report), encoding="utf-8")
        write_actions_csv(report.get("actions", []))

    def print_report(self, report: Mapping[str, Any]) -> None:
        """Print summary."""
        print(format_summary(report))


def file_state(path: Path) -> dict[str, Any]:
    """Return lightweight file state."""
    return {
        "exists": path.exists() and path.stat().st_size > 0,
        "size": path.stat().st_size if path.exists() else 0,
        "rows": len(read_csv_rows(path)) if path.suffix == ".csv" else None,
    }


def format_summary(report: Mapping[str, Any]) -> str:
    """Format short Russian summary."""
    snapshot = report.get("snapshot", {})
    recommendations = report.get("recommendations", {})
    lines = [
        "Adaptive Strategy Advisor",
        "=========================",
        f"Adaptive Mode: {report.get('adaptive_mode', 'N/A')}",
        f"Market Regime: {snapshot.get('market_regime', 'Недостаточно данных')}",
        f"Best Symbol: {snapshot.get('best_symbol', 'N/A')}",
        f"Best Direction: {snapshot.get('best_direction', 'N/A')}",
        f"Days Without Trades: {snapshot.get('days_without_trades', 0)}",
        f"Near Setup Count: {snapshot.get('near_setup_count', 0)}",
        (
            "Dry-run Candidates: "
            f"{snapshot.get('total_dry_run_candidates', 0)} "
            f"(Relaxed Edge {snapshot.get('relaxed_edge_dry_run_candidates', 0)}, "
            f"Long Rebound {snapshot.get('long_rebound_dry_run_candidates', 0)})"
        ),
        f"Average Edge: {snapshot.get('average_near_setup_edge', 0)}",
        f"Average Confidence: {snapshot.get('average_confidence', 0)}%",
        f"Profit Factor: {snapshot.get('profit_factor', 0)}",
        "",
        "Recommendation:",
    ]
    lines.extend(f"- {item}" for item in recommendations.get("what_to_observe", []))
    lines.extend(f"- {item}" for item in recommendations.get("soft_mode", []))
    lines.extend(["", "Почему live-логику нельзя менять сейчас:"])
    lines.extend(
        f"- {item}"
        for item in recommendations.get("why_live_logic_must_not_change", [])
    )
    lines.extend(["", "Forbidden Actions:"])
    lines.extend(f"- {item}" for item in recommendations.get("forbidden_actions", []))
    return "\n".join(lines)


def write_actions_csv(actions: list[Mapping[str, str]]) -> None:
    """Write advisor actions CSV."""
    fieldnames = ["priority", "action", "status", "reason"]
    with ACTIONS_FILE.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for action in actions:
            writer.writerow({field: action.get(field, "") for field in fieldnames})


def main() -> None:
    """Run adaptive strategy advisor."""
    advisor = AdaptiveStrategyAdvisor()
    report = advisor.build_report()
    advisor.save_outputs(report)
    advisor.print_report(report)


if __name__ == "__main__":
    main()
