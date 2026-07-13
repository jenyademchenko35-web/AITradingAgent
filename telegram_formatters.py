"""Compact read-only Telegram UI v5 formatters for AITradingAgent."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from best_candidate_ranker import (
    RankedCandidate,
    explain_selection,
    rank_candidates,
    select_best_candidate,
)
from config import MIN_EDGE, RUN_INTERVAL
from trade_metrics_normalizer import (
    aggregate_trade_metrics,
    is_closed_trade,
    normalize_closed_trades,
)


BASE_DIR = Path(__file__).resolve().parent
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
DECISION_DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
EXPLANATIONS_FILE = BASE_DIR / "decision_explanations.csv"
TRADES_FILE = BASE_DIR / "trades.csv"
STATS_FILE = BASE_DIR / "agent_v3_stats.json"
AI_COACH_FILE = BASE_DIR / "ai_coach_report.json"
DATA_QUALITY_FILE = BASE_DIR / "data_quality_report.json"
PIPELINE_FILE = BASE_DIR / "decision_pipeline_profile_report.json"
OPPORTUNITY_FILE = BASE_DIR / "trade_opportunity_expansion_report.json"
REPLAY_FILE = BASE_DIR / "strategy_replay_report.json"
NEWS_IMPACT_FILE = BASE_DIR / "news_impact_advisor_report.json"
AGENT_VERSION = "v1.0 / Telegram UI v5"
SEPARATOR = "────────────"

DRY_RUN_FILES = [
    BASE_DIR / "protective_filter_dry_run.csv",
    BASE_DIR / "sl_quality_protective_dry_run.csv",
    BASE_DIR / "confidence_sl_quality_d_dry_run.csv",
    BASE_DIR / "ada_opportunity_dry_run.csv",
    BASE_DIR / "doge_link_opportunity_dry_run.csv",
    BASE_DIR / "portfolio_manager_dry_run.csv",
]


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


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object safely."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a value to float."""
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return default


def parse_time(value: str) -> datetime | None:
    """Parse agent timestamps."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def local_time(value: str) -> str:
    """Return a compact local-ish time string."""
    parsed = parse_time(value)
    return parsed.strftime("%H:%M") if parsed else "N/A"


def fmt_pct(value: Any) -> str:
    """Format a percentage consistently."""
    return f"{safe_float(value):.0f}%"


def fmt_score(value: Any) -> str:
    """Format score values consistently."""
    number = safe_float(value)
    return f"{number:.0f}" if number.is_integer() else f"{number:.1f}"


def symbol_short(symbol: str) -> str:
    """Return BTC from BTC/USDT."""
    return (symbol or "N/A").replace("/USDT", "")


def display_direction(row: Mapping[str, str]) -> str:
    """Return LONG/SHORT when a candidate side is known."""
    direction = row.get("direction") or ""
    if direction in {"LONG", "SHORT"}:
        return direction
    winner = row.get("winner") or ""
    if winner in {"LONG", "SHORT"}:
        return winner
    long_total = safe_float(row.get("long_total"))
    short_total = safe_float(row.get("short_total"))
    if long_total > short_total:
        return "LONG"
    if short_total > long_total:
        return "SHORT"
    return direction or winner or "N/A"


def footer() -> str:
    """Return the common v5 footer."""
    return "\n".join(
        [
            "",
            f"Updated: {datetime.now().strftime('%H:%M:%S')}",
            f"Version: {AGENT_VERSION}",
            f"Git: {git_branch()}",
            "Server: ONLINE",
        ]
    )


def latest_by_symbol(rows: Iterable[Mapping[str, str]]) -> dict[str, dict[str, str]]:
    """Return the latest row for every symbol."""
    latest: dict[str, dict[str, str]] = {}
    for row in rows:
        symbol = row.get("symbol", "")
        if not symbol:
            continue
        if symbol not in latest or row.get("timestamp", "") > latest[symbol].get("timestamp", ""):
            latest[symbol] = dict(row)
    return latest


FILTER_NAMES = ("Trend", "Structure", "Momentum", "Risk")
EXACT_TIMESTAMP = "EXACT_TIMESTAMP"
SAME_CYCLE = "SAME_CYCLE"
NO_MATCH = "NO_MATCH"


@dataclass(frozen=True)
class FailedFiltersMatch:
    """Failed filters that are proven to belong to one analysis cycle."""

    filters: tuple[str, ...]
    quality: str


def _symbol_key(value: str) -> str:
    """Normalize a symbol for joins between runtime CSV files."""
    return "".join(character for character in str(value).upper() if character.isalnum())


def _canonical_filters(value: Any) -> list[str]:
    """Parse a delimited filter list into canonical display names."""
    text = str(value or "")
    for delimiter in ("|", ",", ";", "/"):
        text = text.replace(delimiter, " ")
    words = {word.strip().lower() for word in text.split() if word.strip()}
    return [name for name in FILTER_NAMES if name.lower() in words]


def _nearest_symbol_row(
    rows: Iterable[Mapping[str, str]],
    symbol: str,
    timestamp: str = "",
) -> dict[str, str]:
    """Return the row for symbol nearest to a decision timestamp."""
    key = _symbol_key(symbol)
    matches = [dict(row) for row in rows if _symbol_key(row.get("symbol", "")) == key]
    if not matches:
        return {}

    target = parse_time(timestamp)
    if target:
        timed = []
        for row in matches:
            row_time = parse_time(row.get("timestamp", ""))
            if row_time:
                timed.append((abs((row_time - target).total_seconds()), row))
        if timed:
            return min(timed, key=lambda item: item[0])[1]
    return max(matches, key=lambda row: row.get("timestamp", ""))


def _current_cycle_symbol_row(
    rows: Iterable[Mapping[str, str]],
    symbol: str,
    decision_timestamp: str = "",
    cycle_started_at: str = "",
    cycle_finished_at: str = "",
) -> tuple[dict[str, str], str]:
    """Return only a row proven to match the timestamp or current cycle."""
    key = _symbol_key(symbol)
    matches = [dict(row) for row in rows if _symbol_key(row.get("symbol", "")) == key]
    target = parse_time(decision_timestamp)
    if target:
        for row in matches:
            row_time = parse_time(row.get("timestamp", ""))
            if row_time == target:
                return row, EXACT_TIMESTAMP

    cycle_start = parse_time(cycle_started_at)
    cycle_finish = parse_time(cycle_finished_at)
    if cycle_start and cycle_finish:
        current_cycle_rows = []
        for row in matches:
            row_time = parse_time(row.get("timestamp", ""))
            if row_time and cycle_start <= row_time <= cycle_finish:
                current_cycle_rows.append((row_time, row))
        if current_cycle_rows:
            return max(current_cycle_rows, key=lambda item: item[0])[1], SAME_CYCLE

    return {}, NO_MATCH


def failed_filters_match(
    symbol: str,
    decision_timestamp: str = "",
    cycle_started_at: str = "",
    cycle_finished_at: str = "",
    diagnostics_rows: Iterable[Mapping[str, str]] | None = None,
    explanation_rows: Iterable[Mapping[str, str]] | None = None,
) -> FailedFiltersMatch:
    """Return failed filters without falling back to persisted stale rows."""
    diagnostics_source = (
        list(diagnostics_rows)
        if diagnostics_rows is not None
        else read_csv_rows(DIAGNOSTICS_FILE)
    )
    explanations_source = (
        list(explanation_rows)
        if explanation_rows is not None
        else read_csv_rows(EXPLANATIONS_FILE)
    )
    diagnostics, diagnostics_quality = _current_cycle_symbol_row(
        diagnostics_source,
        symbol,
        decision_timestamp,
        cycle_started_at,
        cycle_finished_at,
    )
    explanation, explanation_quality = _current_cycle_symbol_row(
        explanations_source,
        symbol,
        decision_timestamp,
        cycle_started_at,
        cycle_finished_at,
    )

    qualities = {diagnostics_quality, explanation_quality}
    if EXACT_TIMESTAMP in qualities:
        quality = EXACT_TIMESTAMP
    elif SAME_CYCLE in qualities:
        quality = SAME_CYCLE
    else:
        return FailedFiltersMatch((), NO_MATCH)

    failed: set[str] = set()
    for name in FILTER_NAMES:
        if str(diagnostics.get(name.lower(), "")).strip().upper() == "FAIL":
            failed.add(name)
    failed.update(
        _canonical_filters(
            explanation.get("failed") or explanation.get("failed_filters")
        )
    )
    failed.update(_canonical_filters(diagnostics.get("primary_blocker")))
    ordered = tuple(name for name in FILTER_NAMES if name in failed)
    return FailedFiltersMatch(ordered, quality)


def failed_filters_for(
    symbol: str,
    decision_timestamp: str = "",
    diagnostics_rows: Iterable[Mapping[str, str]] | None = None,
    explanation_rows: Iterable[Mapping[str, str]] | None = None,
) -> list[str]:
    """Return real failed filters from diagnostics and explanations."""
    diagnostics_source = (
        list(diagnostics_rows)
        if diagnostics_rows is not None
        else read_csv_rows(DIAGNOSTICS_FILE)
    )
    explanations_source = (
        list(explanation_rows)
        if explanation_rows is not None
        else read_csv_rows(EXPLANATIONS_FILE)
    )
    diagnostics = _nearest_symbol_row(
        diagnostics_source,
        symbol,
        decision_timestamp,
    )
    explanation = _nearest_symbol_row(
        explanations_source,
        symbol,
        decision_timestamp,
    )

    failed: set[str] = set()
    for name in FILTER_NAMES:
        if str(diagnostics.get(name.lower(), "")).strip().upper() == "FAIL":
            failed.add(name)
    failed.update(
        _canonical_filters(
            explanation.get("failed") or explanation.get("failed_filters")
        )
    )
    failed.update(_canonical_filters(diagnostics.get("primary_blocker")))
    return [name for name in FILTER_NAMES if name in failed]


def git_branch() -> str:
    """Read the current git branch without shelling out."""
    head = BASE_DIR / ".git" / "HEAD"
    if not head.exists():
        return "N/A"
    value = head.read_text(encoding="utf-8").strip()
    if value.startswith("ref:"):
        return value.rsplit("/", 1)[-1]
    return value[:7]


def latest_signal_row() -> dict[str, str]:
    """Return the latest signal/debug row available."""
    rows = read_csv_rows(DECISION_DEBUG_FILE) or read_csv_rows(SIGNALS_FILE)
    if not rows:
        return {}
    return max(rows, key=lambda row: row.get("timestamp", ""))


def agent_status() -> dict[str, Any]:
    """Build a small status snapshot from existing runtime files."""
    stats = read_json(STATS_FILE)
    latest = latest_signal_row()
    latest_dt = parse_time(latest.get("timestamp", ""))
    now = datetime.now(timezone.utc)
    online = bool(latest_dt and now - latest_dt <= timedelta(seconds=RUN_INTERVAL * 2 + 600))
    next_cycle = "N/A"
    if latest_dt:
        remaining = latest_dt + timedelta(seconds=RUN_INTERVAL) - now
        if remaining.total_seconds() <= 0:
            next_cycle = "сейчас"
        else:
            minutes, seconds = divmod(int(remaining.total_seconds()), 60)
            next_cycle = f"через {minutes:02d}:{seconds:02d}"
    symbols = latest_by_symbol(read_csv_rows(DECISION_DEBUG_FILE) or read_csv_rows(SIGNALS_FILE))
    return {
        "online": online,
        "latest_time": local_time(latest.get("timestamp", "")),
        "next_cycle": next_cycle,
        "symbols": len(symbols),
        "runs": stats.get("runs", "N/A"),
    }


def closed_trades() -> list[dict[str, str]]:
    """Return closed trade rows."""
    rows = read_csv_rows(TRADES_FILE)
    return [row for row in rows if is_closed_trade(row)]


def open_trades() -> list[dict[str, str]]:
    """Return open trade rows."""
    return [
        row for row in read_csv_rows(TRADES_FILE)
        if row.get("status") == "OPEN"
    ]


def trade_stats() -> dict[str, Any]:
    """Calculate concise metrics with the shared R normalizer."""
    rows = closed_trades()
    metrics = aggregate_trade_metrics(rows)
    normalized = normalize_closed_trades(rows)
    return {
        "closed": metrics["closed_trades"],
        "metrics_trades": metrics["metrics_trades"],
        "incomplete_metrics": metrics["incomplete_metrics"],
        "open": len(open_trades()),
        "wins": metrics["wins"],
        "losses": metrics["losses"],
        "winrate": metrics["winrate"],
        "profit_factor": metrics["profit_factor"],
        "net_r": metrics["net_r"],
        "drawdown_r": metrics["max_drawdown_r"],
        "last_results": [
            str(row.get("result") or "?")
            for row in normalized[-20:]
        ],
    }


def latest_trade_label() -> str:
    """Return a compact last-trade label."""
    rows = read_csv_rows(TRADES_FILE)
    if not rows:
        return "нет"
    row = rows[-1]
    result = row.get("status") or row.get("result") or "N/A"
    return f"{symbol_short(row.get('symbol', 'N/A'))} {row.get('direction', '')} {result}".strip()


def weighted_score(row: Mapping[str, str]) -> float:
    """Return the stronger side score from a decision row."""
    return max(
        safe_float(row.get("weighted_score")),
        safe_float(row.get("long_total")),
        safe_float(row.get("short_total")),
        safe_float(row.get("score")),
    )


def near_setup_category(row: Mapping[str, str]) -> str:
    """Classify how close a row is to directional edge."""
    diff = safe_float(row.get("diff"))
    missing = MIN_EDGE - diff
    if missing <= 2:
        return "VERY_CLOSE"
    if missing <= 5:
        return "CLOSE"
    if missing <= 8:
        return "MEDIUM"
    return "FAR"


def is_near_setup(row: Mapping[str, str]) -> bool:
    """Return True for actionable or near-actionable rows."""
    return rank_candidates([row], min_edge=MIN_EDGE)[0].status in {
        "SETUP",
        "WATCH",
        "NEAR SETUP",
    }


def latest_ranked_candidates(
    rows: Iterable[Mapping[str, str]] | None = None,
) -> list[RankedCandidate]:
    """Return latest candidates sorted by the shared display ranker."""
    latest = latest_by_symbol(
        rows if rows is not None else read_csv_rows(DECISION_DEBUG_FILE)
    )
    return [
        candidate for candidate in rank_candidates(latest.values(), min_edge=MIN_EDGE)
        if candidate.status in {"SETUP", "WATCH", "NEAR SETUP"}
    ]


def latest_candidates() -> list[dict[str, str]]:
    """Return latest candidate rows for legacy callers."""
    latest = latest_by_symbol(read_csv_rows(DECISION_DEBUG_FILE))
    ranked_symbols = [candidate.symbol for candidate in latest_ranked_candidates()]
    return [latest[symbol] for symbol in ranked_symbols if symbol in latest]


def missing_factors(
    symbol: str,
    decision_timestamp: str = "",
    diagnostics_rows: Iterable[Mapping[str, str]] | None = None,
    explanation_rows: Iterable[Mapping[str, str]] | None = None,
) -> str:
    """Return a compact explanation of missing filters."""
    factors = failed_filters_for(
        symbol,
        decision_timestamp,
        diagnostics_rows,
        explanation_rows,
    )
    return " + ".join(factors) if factors else "Directional Edge"


def news_impact_for_symbol(symbol: str) -> dict[str, Any]:
    """Return Shadow News Advisor row for a symbol when available."""
    report = read_json(NEWS_IMPACT_FILE)
    short = symbol_short(symbol)
    for row in report.get("active_ideas", []):
        if symbol_short(str(row.get("symbol", ""))) == short:
            return dict(row)
    return {}


def best_opportunity() -> RankedCandidate | None:
    """Return the current best opportunity row."""
    latest = latest_by_symbol(read_csv_rows(DECISION_DEBUG_FILE))
    if not latest:
        return None
    return select_best_candidate(latest.values(), min_edge=MIN_EDGE)


def market_status(row: Mapping[str, str]) -> str:
    """Return a compact market status label."""
    status = rank_candidates([row], min_edge=MIN_EDGE)[0].status
    if status == "SETUP":
        return "🟢 SETUP"
    if status == "WATCH":
        return "🟡 WATCH"
    if status == "NEAR SETUP":
        return "🔵 NEAR SETUP"
    return "⚪ NO TRADE"


def blocker_distribution() -> list[tuple[str, float]]:
    """Return blocker percentages from the latest diagnostics snapshot."""
    diagnostics = latest_by_symbol(read_csv_rows(DIAGNOSTICS_FILE))
    counts: dict[str, int] = {}
    for row in diagnostics.values():
        blocker = row.get("primary_blocker") or "Directional Edge"
        counts[blocker] = counts.get(blocker, 0) + 1
    total = sum(counts.values()) or 1
    return sorted(
        ((name, count / total * 100) for name, count in counts.items()),
        key=lambda item: item[1],
        reverse=True,
    )


def format_legacy_v5_dashboard() -> str:
    """Return the legacy v5 daily dashboard."""
    status = agent_status()
    stats = trade_stats()
    best = best_opportunity()
    best_symbol = symbol_short(best.symbol) if best else "нет"
    lines = [
        "🟢 AITradingAgent" if status["online"] else "🔴 AITradingAgent",
        "",
        "Agent",
        "ONLINE" if status["online"] else "OFFLINE",
        "Last Scan",
        status["latest_time"],
        "Next Scan",
        status["next_cycle"],
        "Open Trades",
        str(stats["open"]),
        "Best Opportunity",
        best_symbol,
    ]
    if best:
        lines.extend(
            [
                "Confidence",
                fmt_pct(best.confidence),
                "Weighted Score",
                fmt_score(best.weighted_score),
                "Edge",
                f"{fmt_score(best.edge)} / {MIN_EDGE}",
                "Status",
                best.status,
            ]
        )
    lines.extend(
        [
            "",
            "Last Trade",
            latest_trade_label(),
            "",
            format_opportunities_summary(),
        ]
    )
    return "\n".join(lines) + footer()


def format_market() -> str:
    """Return a compact market snapshot."""
    latest = latest_by_symbol(read_csv_rows(DECISION_DEBUG_FILE) or read_csv_rows(SIGNALS_FILE))
    if not latest:
        return "📈 Market\n\nДанных по рынку пока нет." + footer()
    lines = ["📈 Market", ""]
    for symbol in sorted(latest):
        row = latest[symbol]
        lines.append(f"{symbol_short(symbol)}  {market_status(row)}")
    lines.append("")
    lines.append("Нажми монету ниже для деталей.")
    return "\n".join(lines) + footer()


def format_symbol_detail(symbol: str) -> str:
    """Return details for one market symbol."""
    full_symbol = symbol if "/" in symbol else f"{symbol}/USDT"
    latest = latest_by_symbol(read_csv_rows(DECISION_DEBUG_FILE) or read_csv_rows(SIGNALS_FILE))
    row = latest.get(full_symbol)
    if not row:
        return f"📈 {full_symbol}\n\nДанных по символу пока нет." + footer()
    diff = safe_float(row.get("diff"))
    news = news_impact_for_symbol(full_symbol)
    return "\n".join(
        [
            f"📈 {symbol_short(full_symbol)}",
            "",
            market_status(row),
            f"Decision: {row.get('signal', 'N/A')}",
            f"Direction: {display_direction(row)}",
            f"Confidence: {fmt_pct(row.get('confidence'))}",
            f"Weighted Score: {fmt_score(weighted_score(row))}",
            f"Edge: {fmt_score(diff)} / {MIN_EDGE}",
            (
                "Не хватает: "
                f"{missing_factors(full_symbol, row.get('timestamp', ''))}"
            ),
            "",
            "📰 Новости",
            f"Статус: {news.get('news_status', 'NEWS_NEUTRAL')}",
            (
                "Sentiment: "
                f"{news.get('news_sentiment', 'Neutral')} "
                f"{news.get('news_strength', 0)}/5"
            ),
            (
                "Комментарий: "
                f"{news.get('reason', 'Shadow Advisor ждёт свежий отчёт.')}"
            ),
            f"Причина: {row.get('summary', 'N/A')}",
        ]
    ) + footer()


def format_opportunities() -> str:
    """Return the top near-setup candidates."""
    debug_rows = read_csv_rows(DECISION_DEBUG_FILE)
    latest = latest_by_symbol(debug_rows)
    ranked = latest_ranked_candidates(debug_rows)
    if not ranked:
        closest = rank_candidates(latest.values(), min_edge=MIN_EDGE)[:3]
        names = "\n".join(symbol_short(candidate.symbol) for candidate in closest) or "нет"
        return "\n".join(
            [
                "🎯 Opportunities",
                "",
                "Сегодня подходящих сделок нет.",
                "Ближе всех:",
                names,
            ]
        ) + footer()

    medals = ["🥇", "🥈", "🥉", "4.", "5."]
    lines = ["🎯 Opportunities", ""]
    diagnostics_rows = read_csv_rows(DIAGNOSTICS_FILE)
    explanation_rows = read_csv_rows(EXPLANATIONS_FILE)
    for index, candidate in enumerate(ranked[:5]):
        decision_row = latest.get(candidate.symbol, {})
        missing = missing_factors(
            candidate.symbol,
            decision_row.get("timestamp", ""),
            diagnostics_rows,
            explanation_rows,
        )
        lines.extend(
            [
                f"{medals[index]} {symbol_short(candidate.symbol)}",
                candidate.direction,
                "Confidence",
                fmt_pct(candidate.confidence),
                "Weighted Score",
                fmt_score(candidate.weighted_score),
                "Edge",
                f"{fmt_score(candidate.edge)} / {MIN_EDGE}",
                "Статус",
                candidate.status,
                f"Не хватает: {missing}",
                "Причина выбора",
                explain_selection(
                    candidate,
                    min_edge=MIN_EDGE,
                    missing=missing.split(" + "),
                ),
                SEPARATOR,
            ]
        )
    return "\n".join(lines).rstrip(SEPARATOR).rstrip() + footer()


def format_watchlist() -> str:
    """Return only actionable and near-actionable symbols."""
    candidates = latest_candidates()
    if not candidates:
        return "📋 Watchlist\n\nSETUP, WATCH и NEAR SETUP сейчас нет." + footer()
    lines = ["📋 Watchlist", ""]
    for row in candidates:
        symbol = symbol_short(row.get("symbol", "N/A"))
        signal = row.get("signal", "N/A")
        label = market_status(row) if signal == "NO TRADE" else market_status(row)
        lines.append(
            f"{symbol}: {label} | Conf {fmt_pct(row.get('confidence'))} | "
            f"Edge {fmt_score(row.get('diff'))}/{MIN_EDGE}"
        )
    return "\n".join(lines) + footer()


def format_statistics() -> str:
    """Return short trade statistics."""
    stats = trade_stats()
    icons = {"WIN": "🟢", "LOSS": "🔴", "W": "🟢", "L": "🔴"}
    last = "".join(icons.get(result, "⚪") for result in stats["last_results"]) or "нет"
    return "\n".join(
        [
            "📊 Статистика",
            "",
            f"Закрытых сделок: {stats['closed']}",
            f"Winrate: {stats['winrate']:.2f}%",
            f"Profit Factor: {stats['profit_factor']:.4f}",
            f"Net R: {stats['net_r']:.4f}",
            f"Max Drawdown: {stats['drawdown_r']:.4f} R",
            f"Incomplete metrics: {stats['incomplete_metrics']}",
            "Последние 20 результатов",
            last,
        ]
    ) + footer()


def format_trades() -> str:
    """Return compact open trade status."""
    rows = open_trades()
    if not rows:
        return "📂 Trades\n\nОткрытых сделок нет.\n\nLast Trade\n" + latest_trade_label() + footer()
    lines = ["📂 Trades", ""]
    for row in rows:
        lines.extend(
            [
                f"{symbol_short(row.get('symbol', 'N/A'))} {row.get('direction', '')}",
                f"Entry: {row.get('entry', 'N/A')}",
                f"SL: {row.get('stop_loss', 'N/A')}",
                f"TP: {row.get('take_profit', 'N/A')}",
                SEPARATOR,
            ]
        )
    return "\n".join(lines).rstrip(SEPARATOR).rstrip() + footer()


def format_ai_coach() -> str:
    """Return recommendation-oriented coach text."""
    latest = latest_candidates()
    quality = read_json(DATA_QUALITY_FILE).get("status", "N/A")
    pipeline = read_json(PIPELINE_FILE)
    distribution = blocker_distribution()
    reason = pipeline.get("score_zero", {}).get("main_reason")
    if not reason and distribution:
        reason = distribution[0][0]
    promising = "\n".join(symbol_short(row.get("symbol", "")) for row in latest[:3]) or "нет"
    blocker_lines = [f"{name}: {percent:.0f}%" for name, percent in distribution[:3]]
    if not blocker_lines:
        blocker_lines = ["Directional Edge: N/A"]
    return "\n".join(
        [
            "🧠 AI Coach",
            "",
            "Сегодня",
            "Рынок нейтральный.",
            "Причина отсутствия сделок:",
            *(blocker_lines or [str(reason)]),
            "",
            "Рекомендация",
            "Стратегию не менять.",
            "Продолжать наблюдать:",
            promising,
            f"Data Quality: {quality}",
        ]
    ) + footer()


def format_opportunities_summary() -> str:
    """Return today's opportunity funnel summary."""
    rows = read_csv_rows(SIGNALS_FILE)
    today = datetime.now(timezone.utc).date()
    today_rows = [
        row for row in rows
        if (parse_time(row.get("timestamp", "")) or datetime.min.replace(tzinfo=timezone.utc)).date() == today
    ]
    if not today_rows:
        today_rows = rows[-50:]
    no_trade = sum(1 for row in today_rows if row.get("signal") == "NO TRADE")
    watch = sum(1 for row in today_rows if row.get("signal") == "WATCH")
    setup = sum(1 for row in today_rows if row.get("signal") in {"SETUP", "HIGH PRIORITY"})
    near = len(latest_candidates())
    dry_runs = sum(max(0, len(read_csv_rows(path))) for path in DRY_RUN_FILES)
    return "\n".join(
        [
            "🎯 Opportunities Summary",
            f"Сегодня анализов: {len(today_rows)}",
            f"NO TRADE: {no_trade}",
            f"Near Setup: {near}",
            f"WATCH: {watch}",
            f"SETUP/HIGH PRIORITY: {setup}",
            f"Open Trades: {len(open_trades())}",
            f"Dry Run Candidates: {dry_runs}",
        ]
    )


def format_settings() -> str:
    """Return read-only UI settings screen."""
    return "\n".join(
        [
            "⚙️ Settings",
            "",
            "Режим: read-only интерфейс",
            f"Интервал агента: {RUN_INTERVAL} sec",
            f"MIN_EDGE: {MIN_EDGE}",
            "Стратегия: без изменений",
            "",
            "Изменения параметров выполняются только вручную после анализа.",
        ]
    ) + footer()


def format_developer() -> str:
    """Return developer menu intro."""
    return "\n".join(
        [
            "🛠 Developer",
            "",
            "Здесь собраны редкие исследовательские разделы:",
            "Replay, Diagnostics, Research, Experiments, Dry Runs, Reports.",
            "Market Intelligence: /news, /heatmap, /intelligence, /memory BTC, /context.",
            "",
            "Ежедневная работа вынесена в Dashboard, Market и Opportunities.",
        ]
    ) + footer()


def format_dry_run() -> str:
    """Return dry-run candidate counts."""
    lines = ["🧪 Dry Run", ""]
    for path in DRY_RUN_FILES:
        lines.append(f"{path.name}: {len(read_csv_rows(path))}")
    return "\n".join(lines) + footer()


def format_reports_status() -> str:
    """Return compact report availability status."""
    reports = [
        DATA_QUALITY_FILE,
        PIPELINE_FILE,
        OPPORTUNITY_FILE,
        REPLAY_FILE,
        AI_COACH_FILE,
        BASE_DIR / "meta_strategy_validation_report.json",
        BASE_DIR / "market_news_feed.json",
        BASE_DIR / "market_heatmap_report.json",
        BASE_DIR / "market_intelligence_report.json",
        BASE_DIR / "trade_market_context.csv",
    ]
    lines = ["📄 Reports", ""]
    for path in reports:
        status = "OK" if path.exists() and path.stat().st_size > 0 else "missing"
        lines.append(f"{path.name}: {status}")
    return "\n".join(lines) + footer()


def market_symbols() -> list[str]:
    """Return latest symbols for market detail buttons."""
    latest = latest_by_symbol(read_csv_rows(DECISION_DEBUG_FILE) or read_csv_rows(SIGNALS_FILE))
    return sorted(latest)
