"""Compact read-only Telegram UI v5 formatters for AITradingAgent."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from config import MIN_EDGE, RUN_INTERVAL


BASE_DIR = Path(__file__).resolve().parent
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
DECISION_DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
TRADES_FILE = BASE_DIR / "trades.csv"
STATS_FILE = BASE_DIR / "agent_v3_stats.json"
AI_COACH_FILE = BASE_DIR / "ai_coach_report.json"
DATA_QUALITY_FILE = BASE_DIR / "data_quality_report.json"
PIPELINE_FILE = BASE_DIR / "decision_pipeline_profile_report.json"
OPPORTUNITY_FILE = BASE_DIR / "trade_opportunity_expansion_report.json"
REPLAY_FILE = BASE_DIR / "strategy_replay_report.json"
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
    return [
        row for row in rows
        if (row.get("status") or row.get("result")) in {"WIN", "LOSS"}
    ]


def open_trades() -> list[dict[str, str]]:
    """Return open trade rows."""
    return [
        row for row in read_csv_rows(TRADES_FILE)
        if row.get("status") == "OPEN"
    ]


def trade_stats() -> dict[str, Any]:
    """Calculate concise trade metrics."""
    rows = closed_trades()
    wins = [row for row in rows if (row.get("status") or row.get("result")) == "WIN"]
    losses = [row for row in rows if (row.get("status") or row.get("result")) == "LOSS"]
    pnls = [safe_float(row.get("pnl")) for row in rows]
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
        "closed": len(rows),
        "open": len(open_trades()),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": (len(wins) / len(rows) * 100) if rows else 0.0,
        "profit_factor": (gross_profit / gross_loss) if gross_loss else 0.0,
        "roi": sum(pnls),
        "drawdown": max_drawdown,
        "last_results": [
            row.get("status") or row.get("result") or "?"
            for row in rows[-20:]
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
    signal = row.get("signal", "")
    if signal in {"WATCH", "SETUP", "HIGH PRIORITY"}:
        return True
    return (
        signal == "NO TRADE"
        and safe_float(row.get("score")) == 0
        and safe_float(row.get("confidence")) >= 60
        and weighted_score(row) >= 18
        and safe_float(row.get("diff")) >= MIN_EDGE - 8
    )


def latest_candidates() -> list[dict[str, str]]:
    """Return latest candidate rows sorted by closeness to SETUP."""
    latest = latest_by_symbol(read_csv_rows(DECISION_DEBUG_FILE))
    candidates = [row for row in latest.values() if is_near_setup(row)]
    return sorted(
        candidates,
        key=lambda row: (
            row.get("signal") in {"HIGH PRIORITY", "SETUP", "WATCH"},
            safe_float(row.get("diff")),
            safe_float(row.get("confidence")),
            weighted_score(row),
        ),
        reverse=True,
    )


def missing_factors(symbol: str) -> str:
    """Return a compact explanation of missing filters."""
    diagnostics = latest_by_symbol(read_csv_rows(DIAGNOSTICS_FILE)).get(symbol, {})
    failed = [
        name for name in ("trend", "structure", "momentum", "risk")
        if diagnostics.get(name) == "FAIL"
    ]
    blocker = diagnostics.get("primary_blocker")
    factors = [item.title() for item in failed]
    if blocker and blocker not in factors:
        factors.append(blocker)
    return ", ".join(factors[:3]) if factors else "Directional Edge"


def best_opportunity() -> dict[str, str]:
    """Return the current best opportunity row."""
    candidates = latest_candidates()
    if candidates:
        return candidates[0]
    latest = latest_by_symbol(read_csv_rows(DECISION_DEBUG_FILE))
    if not latest:
        return {}
    return max(
        latest.values(),
        key=lambda row: (
            safe_float(row.get("confidence")),
            weighted_score(row),
            safe_float(row.get("diff")),
        ),
    )


def market_status(row: Mapping[str, str]) -> str:
    """Return a compact market status label."""
    signal = row.get("signal", "N/A")
    if signal in {"SETUP", "HIGH PRIORITY"}:
        return "🟢 SETUP"
    if signal == "WATCH":
        return "🟡 WATCH"
    if is_near_setup(row):
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


def format_dashboard() -> str:
    """Return the v5 daily dashboard."""
    status = agent_status()
    stats = trade_stats()
    best = best_opportunity()
    best_symbol = symbol_short(best.get("symbol", "нет")) if best else "нет"
    diff = safe_float(best.get("diff")) if best else 0.0
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
                fmt_pct(best.get("confidence")),
                "Edge",
                f"{fmt_score(diff)} / {MIN_EDGE}",
                "Status",
                market_status(best).replace("🔵 ", "").replace("⚪ ", ""),
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
            "Missing:",
            missing_factors(full_symbol),
            f"Причина: {row.get('summary', 'N/A')}",
        ]
    ) + footer()


def format_opportunities() -> str:
    """Return the top near-setup candidates."""
    candidates = latest_candidates()
    if not candidates:
        latest = latest_by_symbol(read_csv_rows(DECISION_DEBUG_FILE))
        closest = sorted(
            latest.values(),
            key=lambda row: (
                safe_float(row.get("confidence")),
                weighted_score(row),
                safe_float(row.get("diff")),
            ),
            reverse=True,
        )[:3]
        names = "\n".join(symbol_short(row.get("symbol", "")) for row in closest) or "нет"
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
    for index, row in enumerate(candidates[:5]):
        diff = safe_float(row.get("diff"))
        lines.extend(
            [
                f"{medals[index]} {symbol_short(row.get('symbol', 'N/A'))}",
                display_direction(row),
                "Confidence",
                fmt_pct(row.get("confidence")),
                "Weighted Score",
                fmt_score(weighted_score(row)),
                "Edge",
                f"{fmt_score(diff)} / {MIN_EDGE}",
                "Missing",
                missing_factors(row.get("symbol", "")),
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
            "📊 Statistics",
            "",
            "Winrate",
            f"{stats['winrate']:.1f}%",
            "Profit Factor",
            f"{stats['profit_factor']:.2f}",
            "ROI",
            f"{stats['roi']:.2f}",
            "Drawdown",
            f"{stats['drawdown']:.2f}",
            "Последние 20 результатов",
            last,
            "",
            f"Закрытых сделок: {stats['closed']}",
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
