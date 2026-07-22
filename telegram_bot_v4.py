"""Telegram assistant for AITradingAgent v3."""

from __future__ import annotations

import csv
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional

from telegram import InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Conflict, NetworkError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from adaptive_research.formatter import (
    format_overview as format_adaptive_overview,
    format_recommendations as format_adaptive_recommendations,
    format_stages as format_adaptive_stages,
    format_status as format_adaptive_status,
)
from config import RUN_INTERVAL
from calibration_report import build_calibration_report, save_report
from decision_diagnostics import DecisionDiagnostics
from dashboard.dashboard_formatter import format_dashboard as format_live_dashboard
from live_monitor.formatters import (
    format_live as format_live_monitor,
    format_system as format_live_system,
    load_state as load_live_monitor_state,
    state_age_text as live_state_age_text,
)
from notification_manager import save_chat_id
from execution_simulator import (
    ExecutionSimulator,
    format_execution,
    load_execution_report,
)
from loss_attribution import (
    LossAttribution,
    format_loss_analysis,
    load_loss_report,
)
from signal_quality_analyzer import (
    SignalQualityAnalyzer,
    format_signal_quality,
    load_signal_quality,
)
from decision_engine_v2 import (
    DecisionEngineV2,
    format_decision_v2,
    load_decision_v2,
)
from research_data_quality import (
    build_decision_snapshot,
    format_data_quality as format_research_data_quality,
    run_backfill_pipeline,
)
from promotion_gate import format_telegram as format_promotion_gate
from decision_intelligence import format_telegram as format_decision_learning
from portfolio_manager import (
    PortfolioManager,
    format_portfolio,
    load_portfolio_report,
)
from news_observer.formatter import format_telegram as format_news_v2
from research_consensus.consensus_formatter import (
    format_group as format_consensus_group,
    format_overview as format_consensus_overview,
    format_summary_command as format_consensus_summary,
)
from research_orchestrator.formatter import (
    format_telegram as format_research_orchestrator,
)
from research_dashboard import (
    format_telegram as format_research_dashboard,
    read_dashboard as read_research_dashboard,
)
from report_synchronization import (
    datasource_status,
    synchronize_reports,
)
from telegram_formatters import (
    format_ai_coach as v5_format_ai_coach,
    format_developer as v5_format_developer,
    format_dry_run as v5_format_dry_run,
    footer as v5_footer,
    format_market as v5_format_market,
    format_opportunities as v5_format_opportunities,
    format_reports_status as v5_format_reports_status,
    format_settings as v5_format_settings,
    format_statistics as v5_format_statistics,
    format_symbol_detail as v5_format_symbol_detail,
    format_trades as v5_format_trades,
    format_watchlist as v5_format_watchlist,
    failed_filters_for as v5_failed_filters_for,
    market_symbols as v5_market_symbols,
)
from telegram_handlers import (
    BOT_COMMANDS_V5,
    developer_keyboard as v5_developer_keyboard,
    main_keyboard as v5_main_keyboard,
    market_keyboard as v5_market_keyboard,
)
from trade_metrics_normalizer import (
    aggregate_trade_metrics,
    is_closed_trade,
    normalize_closed_trades,
)
from trade_registry import (
    format_summary as format_data_quality_summary,
    read_quality_report,
)


try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(dotenv_path: str = ".env") -> None:
        path = Path(dotenv_path)
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


BASE_DIR = Path(__file__).resolve().parent
BOT_TOKEN = os.getenv("BOT_TOKEN")

SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
DECISION_DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
EXPLANATIONS_FILE = BASE_DIR / "decision_explanations.csv"
CALIBRATION_FILE = BASE_DIR / "calibration_report.json"
RESEARCH_REPORT_FILE = BASE_DIR / "strategy_research_report.json"
RESEARCH_SUMMARY_FILE = BASE_DIR / "strategy_research_summary.txt"
EXPERIMENTS_REPORT_FILE = BASE_DIR / "strategy_experiments_report.json"
EXPERIMENTS_SUMMARY_FILE = BASE_DIR / "strategy_experiments_summary.txt"
EXPERIMENTS_RESULTS_FILE = BASE_DIR / "strategy_experiments_results.csv"
CANDIDATE_WEIGHTS_FILE = BASE_DIR / "candidate_weights.json"
AUTO_LEARNING_FILE = BASE_DIR / "auto_learning_recommendation.json"
DATA_QUALITY_FILE = BASE_DIR / "data_quality_report.json"
FILTERS_REPORT_FILE = BASE_DIR / "filter_effectiveness_report.json"
MARKET_REGIME_FILE = BASE_DIR / "market_regime_report.json"
AI_COACH_REPORT_FILE = BASE_DIR / "ai_coach_report.json"
AI_COACH_SUMMARY_FILE = BASE_DIR / "ai_coach_summary.txt"
POST_TRADE_REPORT_FILE = BASE_DIR / "post_trade_analysis_report.json"
POST_TRADE_SUMMARY_FILE = BASE_DIR / "post_trade_analysis_summary.txt"
POST_TRADE_TRADES_FILE = BASE_DIR / "post_trade_analysis_trades.csv"
MARKET_NEWS_FILE = BASE_DIR / "market_news_feed.json"
MARKET_NEWS_SUMMARY_FILE = BASE_DIR / "market_news_summary.txt"
MARKET_NEWS_SOURCES_FILE = BASE_DIR / "market_news_sources.json"
MARKET_NEWS_HEALTH_FILE = BASE_DIR / "market_news_health.json"
MARKET_HEATMAP_FILE = BASE_DIR / "market_heatmap_report.json"
MARKET_INTELLIGENCE_FILE = BASE_DIR / "market_intelligence_report.json"
MARKET_INTELLIGENCE_SUMMARY_FILE = BASE_DIR / "market_intelligence_summary.txt"
LIVE_MONITOR_STATE_FILE = BASE_DIR / "live_monitor_state.json"
TRADE_MARKET_CONTEXT_FILE = BASE_DIR / "trade_market_context.csv"
TRADE_MEMORY_SUMMARY_FILE = BASE_DIR / "trade_memory_summary.txt"
STRATEGY_LAB_REPORT_FILE = BASE_DIR / "strategy_lab_report.json"
STRATEGY_LAB_SUMMARY_FILE = BASE_DIR / "strategy_lab_summary.txt"
HYPOTHESIS_REPORT_FILE = BASE_DIR / "hypothesis_report.json"
HYPOTHESIS_SUMMARY_FILE = BASE_DIR / "hypothesis_summary.txt"
TRADE_REPLAY_REPORT_FILE = BASE_DIR / "trade_replay_report.json"
TRADE_REPLAY_SUMMARY_FILE = BASE_DIR / "replay_summary.txt"
SHADOW_REPLAY_REPORT_FILE = BASE_DIR / "shadow_replay_report.json"
SHADOW_REPLAY_SUMMARY_FILE = BASE_DIR / "shadow_replay_summary.txt"
RESEARCH_CONSENSUS_REPORT_FILE = BASE_DIR / "research_consensus_report.json"
RESEARCH_ORCHESTRATOR_REPORT_FILE = BASE_DIR / "research_orchestrator_report.json"
ADAPTIVE_RESEARCH_REPORT_FILE = BASE_DIR / "adaptive_research_report.json"
ADAPTIVE_RESEARCH_STATE_FILE = BASE_DIR / "adaptive_research_state.json"
EXPERIMENT_PROMOTION_REPORT_FILE = BASE_DIR / "experiment_promotion_report.json"
WALK_FORWARD_REPORT_FILE = BASE_DIR / "reports" / "walk_forward.json"
STATS_FILE = BASE_DIR / "agent_v3_stats.json"
TRADES_FILE = BASE_DIR / "trades.csv"
WEIGHTS_FILE = BASE_DIR / "strategy_weights.json"
VENV_PYTHON = BASE_DIR / "venv" / "bin" / "python"

MAX_MESSAGE_LENGTH = 3900
FRESHNESS_GRACE_SECONDS = 60 * 60
NEWS_UPDATE_INTERVAL_SECONDS = 30 * 60
NEWS_WARNING_SECONDS = 2 * 60 * 60
NEWS_STALE_SECONDS = 6 * 60 * 60
LOCAL_TZ = timezone(timedelta(hours=3), "MSK")
BOT_COMMANDS = BOT_COMMANDS_V5


load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read CSV rows, ignoring repeated headers and empty rows."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    clean_rows = []
    for row in rows:
        if not row or not any(row.values()):
            continue
        if "timestamp" in row and row.get("timestamp") in ("", "timestamp"):
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


def run_readonly_module(
    script_name: str,
    *args: str,
    timeout: int = 90,
) -> Optional[str]:
    """Run a read-only analytics module through the project venv."""
    if not VENV_PYTHON.exists():
        return (
            "Не найден проектный Python: "
            f"{VENV_PYTHON}. Сначала проверь venv."
        )
    script_path = BASE_DIR / script_name
    if not script_path.exists():
        return f"Модуль {script_name} не найден."
    try:
        subprocess.run(
            [str(VENV_PYTHON), str(script_path), *args],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Модуль {script_name} не успел завершиться за {timeout} сек."
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        return f"Не удалось выполнить {script_name}: {details}"
    return None


def parse_time(value: str) -> Optional[datetime]:
    """Parse ISO timestamps used by the agent logs."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_time(value: str) -> str:
    """Format an ISO timestamp for compact Telegram output."""
    parsed = parse_time(value)
    if parsed is None:
        return value or "N/A"
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def format_local_time(value: datetime) -> str:
    """Format a datetime in Moscow time for Telegram."""
    return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M MSK")


def news_human_age(seconds: float) -> str:
    """Return a compact Russian age string."""
    if seconds < 60:
        return "меньше минуты назад"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    rest_minutes = minutes % 60
    if hours < 24:
        return f"{hours} ч {rest_minutes} мин назад"
    days = hours // 24
    rest_hours = hours % 24
    return f"{days} д {rest_hours} ч назад"


def news_last_update() -> Optional[datetime]:
    """Return last real news update time from JSON generated_at or file mtime."""
    report = read_json(MARKET_NEWS_FILE)
    metadata = report.get("metadata", {})
    metadata = metadata if isinstance(metadata, Mapping) else {}
    generated_at = parse_time(str(
        metadata.get("last_success_at")
        or report.get("last_success_at")
        or metadata.get("generated_at")
        or report.get("generated_at", "")
    ))
    if generated_at:
        return generated_at
    candidates = [
        path for path in (MARKET_NEWS_FILE, MARKET_NEWS_SUMMARY_FILE)
        if path.exists() and path.stat().st_size > 0
    ]
    if not candidates:
        return None
    latest_mtime = max(path.stat().st_mtime for path in candidates)
    return datetime.fromtimestamp(latest_mtime, tz=timezone.utc)


def news_freshness() -> Dict[str, Any]:
    """Build Telegram freshness metadata for market news."""
    updated_at = news_last_update()
    observer_status = str(
        read_json(MARKET_NEWS_FILE).get("status", "NO_DATA")
    ).upper()
    if updated_at is None:
        return {
            "updated_text": "нет данных",
            "age_text": "нет данных",
            "next_update_text": "неизвестно",
            "warning": "Market News пока не обновлялись.",
        }
    age_seconds = max(
        0.0,
        (datetime.now(timezone.utc) - updated_at).total_seconds(),
    )
    remaining = max(0, int(NEWS_UPDATE_INTERVAL_SECONDS - age_seconds))
    if remaining <= 0:
        next_update = "примерно сейчас"
    else:
        next_update = f"примерно через {(remaining + 59) // 60} мин"
    warning = ""
    if observer_status == "STALE":
        warning = "🔴 News data: STALE"
    elif observer_status in {"NO_DATA", "FAILED"}:
        warning = "🔴 Свежих новостных данных нет"
    elif age_seconds >= NEWS_STALE_SECONDS:
        warning = "🔴 Новости устарели"
    elif age_seconds >= NEWS_WARNING_SECONDS:
        warning = "⚠️ Новости могли устареть"
    return {
        "updated_text": format_local_time(updated_at),
        "age_text": news_human_age(age_seconds),
        "next_update_text": next_update,
        "warning": warning,
    }


def news_freshness_lines(include_next: bool = True) -> List[str]:
    """Return freshness lines for Telegram messages."""
    freshness = news_freshness()
    lines = [
        "Последнее обновление:",
        freshness["updated_text"],
        "Обновлено:",
        freshness["age_text"],
    ]
    if include_next:
        lines.extend(["Следующее обновление:", freshness["next_update_text"]])
    if freshness["warning"]:
        lines.append(str(freshness["warning"]))
    return lines


def inject_news_freshness_into_summary(text: str) -> str:
    """Insert news freshness right after the News block in intelligence text."""
    lines = text.splitlines()
    freshness = news_freshness()
    insert = ["Обновлено:", freshness["age_text"]]
    if freshness["warning"]:
        insert.append(str(freshness["warning"]))
    for index, line in enumerate(lines):
        if line.strip() == "Новости":
            position = min(index + 2, len(lines))
            lines[position:position] = insert
            return "\n".join(lines)
    lines.extend(["", "Новости", "Обновлено:", freshness["age_text"]])
    if freshness["warning"]:
        lines.append(str(freshness["warning"]))
    return "\n".join(lines)


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert values from CSV to float."""
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return default


def weights_sum(weights: Mapping[str, Any]) -> float:
    """Return the sum of the four main weights."""
    return round(
        sum(
            safe_float(weights.get(key))
            for key in ("trend", "structure", "momentum", "risk")
        ),
        6,
    )


def weights_status(weights: Mapping[str, Any]) -> str:
    """Return VALID when weights sum to 1.0, otherwise INVALID."""
    return "VALID" if weights_sum(weights) == 1.0 else "INVALID"


def latest_by_symbol(rows: Iterable[Mapping[str, str]]) -> Dict[str, Dict[str, str]]:
    """Return the latest row per symbol by timestamp."""
    latest: Dict[str, Dict[str, str]] = {}
    for row in rows:
        symbol = row.get("symbol", "")
        timestamp = row.get("timestamp", "")
        if not symbol:
            continue
        if symbol not in latest or timestamp > latest[symbol].get("timestamp", ""):
            latest[symbol] = dict(row)
    return latest


def normalize_symbol(symbol: str) -> str:
    """Normalize BTC or BTC/USDT into BTC/USDT where possible."""
    clean = symbol.strip().upper()
    if not clean:
        return ""
    if "/" in clean:
        return clean
    return f"{clean}/USDT"


def normalize_blocker(value: str) -> Optional[str]:
    """Normalize Telegram blocker arguments to supported simulator values."""
    clean = (value or "").strip()
    if not clean:
        return "Momentum"
    upper = clean.upper()
    if upper == "ALL":
        return "ALL"
    mapping = {
        "MOMENTUM": "Momentum",
        "STRUCTURE": "Structure",
        "RISK": "Risk",
        "TREND": "Trend",
    }
    return mapping.get(upper)


def load_weights(symbol: str) -> Dict[str, float]:
    """Load DecisionEngine weights for diagnostics reconstruction."""
    data = read_json(WEIGHTS_FILE)
    weights = data.get(symbol) or data.get("global") or {}
    return {
        "trend": safe_float(weights.get("trend"), 1.0),
        "structure": safe_float(weights.get("structure"), 1.0),
        "momentum": safe_float(weights.get("momentum"), 1.0),
        "risk": safe_float(weights.get("risk"), 1.0),
    }


def main_keyboard() -> InlineKeyboardMarkup:
    """Build the main inline keyboard."""
    return v5_main_keyboard()


def localize_calibration_item(text: str) -> str:
    """Translate known calibration recommendations to Russian."""
    replacements = {
        "is the most frequent primary blocker": "чаще всего выступает главным блокером",
        "of decisions": "всех решений",
        "Score threshold may be strict: many non-actionable decisions cluster between WATCH and SETUP potential scores.": (
            "Порог Score может быть слишком жёстким: много неактивных решений "
            "сосредоточены между WATCH и потенциальным SETUP."
        ),
        "Average potential score is near WATCH/SETUP territory; review score thresholds only after confirming blocker quality.": (
            "Средний potential score уже рядом с зоной WATCH/SETUP; "
            "пересматривать пороги Score стоит только после подтверждения "
            "качества блокеров."
        ),
        "Keep these symbols in the watchlist: ": "Оставить в watchlist: ",
        "Risk filter looks strict. Review Risk thresholds and price-zone rules before changing score thresholds.": (
            "Фильтр Risk выглядит слишком жёстким. Сначала стоит проверить "
            "пороги Risk и правила price-zone, а уже потом менять Score."
        ),
        "No symbols repeatedly approached SETUP yet. Collect more diagnostics before narrowing the watchlist.": (
            "Пока нет символов, которые стабильно подходят близко к SETUP. "
            "Нужно собрать больше diagnostics перед сужением watchlist."
        ),
        "decision_diagnostics.csv is missing or empty. Run the agent first to collect diagnostics before calibrating Risk or Score.": (
            "Файл decision_diagnostics.csv пуст или отсутствует. Сначала нужно "
            "запустить агент и собрать diagnostics перед калибровкой Risk или Score."
        ),
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def localize_diagnostics(text: str) -> str:
    """Translate diagnostics output to Russian while keeping trading terms."""
    replacements = {
        "Decision Diagnostics": "🧠 Диагностика решения",
        "Decision        :": "Решение        :",
        "Candidate       :": "Кандидат       :",
        "Filters": "Фильтры",
        "Primary Blocker": "Главный блокер",
        "Contributions": "Вклад движков",
        "Lost Score": "Потерянный Score",
        "Potential Score :": "Потенциальный Score :",
        "Actual Score    :": "Фактический Score   :",
        "Lost Score      :": "Потерянный Score    :",
        "If One Filter Passed": "Если бы один фильтр прошёл",
        "Without ": "Без ",
        "None": "Нет",
        "No decision_debug data for ": "Нет данных decision_debug для ",
        "Diagnostics": "🧠 Диагностика",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def localize_research_item(text: str) -> str:
    """Translate known research recommendations to Russian."""
    replacements = {
        "Check whether Momentum is too strict; it is the most frequent blocker.": (
            "Проверь, не слишком ли жёсткий Momentum: сейчас он чаще всего блокирует решения."
        ),
        "Review Trend contribution gaps first; it produces the largest lost contribution.": (
            "Сначала стоит проверить провалы по Trend: именно он даёт самый большой потерянный вклад."
        ),
        "repeatedly shows weak Momentum; verify whether this symbol belongs in the active universe.": (
            "стабильно показывает слабый Momentum; стоит проверить, нужно ли держать этот символ в активном списке."
        ),
        "Keep ": "Оставить ",
        " in backtest research first; they most often approach SETUP.": (
            " в приоритете для backtest-исследований: они чаще всего подходят близко к SETUP."
        ),
        "There is a noticeable SHORT bias; test market-regime sensitivity in backtest.": (
            "Есть заметный перекос в сторону SHORT; это стоит проверить в backtest на чувствительность к рыночному режиму."
        ),
        "Closed trade performance is weak; test stricter entry selection in backtest before changing live weights.": (
            "Результат по закрытым сделкам слабый; сначала стоит проверить более строгий отбор входов в backtest, а не менять live-веса."
        ),
        "Consider moving backtest research knobs into config only after the current weak filters are confirmed statistically.": (
            "Параметры для исследовательских backtest лучше выносить в config только после статистического подтверждения текущих слабых фильтров."
        ),
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def ensure_experiments_report() -> Optional[str]:
    """Build strategy experiments artifacts on demand if they are missing."""
    files_ready = all(
        path.exists() and path.stat().st_size > 0
        for path in (
            EXPERIMENTS_REPORT_FILE,
            EXPERIMENTS_SUMMARY_FILE,
            EXPERIMENTS_RESULTS_FILE,
        )
    )
    if files_ready:
        return None
    if not VENV_PYTHON.exists():
        return (
            "Не найден проектный Python: "
            f"{VENV_PYTHON}. Сначала проверь venv."
        )
    try:
        subprocess.run(
            [str(VENV_PYTHON), str(BASE_DIR / "strategy_experiments.py")],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        return f"Не удалось построить experiments-отчёт: {details}"
    return None


def format_experiment_metrics(
    title: str,
    scenario: Mapping[str, Any],
) -> List[str]:
    """Format one scenario block for Telegram."""
    metrics = scenario.get("trade_metrics", {})
    return [
        title,
        f"Сценарий: {scenario.get('scenario', 'N/A')}",
        (
            "Сигналы: "
            f"{scenario.get('signals_count', 0)} | "
            f"SETUP {scenario.get('setup_count', 0)} | "
            f"HIGH PRIORITY {scenario.get('high_priority_count', 0)}"
        ),
        f"NO TRADE: {scenario.get('no_trade_count', 0)}",
        f"Winrate: {metrics.get('winrate', 0)}%",
        f"Average PnL: {metrics.get('average_pnl', 0)}",
        f"PF: {metrics.get('profit_factor', 0)}",
        f"Max DD: {metrics.get('max_drawdown', 0)}",
    ]


def format_experiments(full: bool = False) -> str:
    """Format strategy experiments for Telegram."""
    error = ensure_experiments_report()
    if error:
        return f"🧪 Experiments\n\n{error}"

    report = read_json(EXPERIMENTS_REPORT_FILE)
    if not report:
        return (
            "🧪 Experiments\n\n"
            "Файл strategy_experiments_report.json пуст или повреждён."
        )

    summary_generated_at = "N/A"
    if EXPERIMENTS_SUMMARY_FILE.exists():
        summary_text = EXPERIMENTS_SUMMARY_FILE.read_text(encoding="utf-8")
        for line in summary_text.splitlines():
            if line.startswith("Generated at:"):
                summary_generated_at = line.split(":", 1)[1].strip()
                break

    results_rows = read_csv_rows(EXPERIMENTS_RESULTS_FILE)
    baseline_row = next(
        (row for row in report.get("results", []) if row.get("scenario") == "baseline"),
        {},
    )
    if not baseline_row and results_rows:
        baseline_csv = next(
            (row for row in results_rows if row.get("scenario") == "baseline"),
            {},
        )
        baseline_row = {
            "scenario": baseline_csv.get("scenario", "baseline"),
            "signals_count": baseline_csv.get("signals_count", 0),
            "setup_count": baseline_csv.get("setup_count", 0),
            "high_priority_count": baseline_csv.get("high_priority_count", 0),
            "no_trade_count": baseline_csv.get("no_trade_count", 0),
            "trade_metrics": {
                "winrate": baseline_csv.get("winrate", 0),
                "average_pnl": baseline_csv.get("average_pnl", 0),
                "profit_factor": baseline_csv.get("profit_factor", 0),
                "max_drawdown": baseline_csv.get("max_drawdown", 0),
            },
        }

    best = report.get("best_scenario", {})
    worst = report.get("worst_scenario", {})

    baseline_metrics = baseline_row.get("trade_metrics", {})
    best_metrics = best.get("trade_metrics", {})
    compare_lines = [
        "Сравнение baseline vs best:",
        (
            "Winrate: "
            f"{baseline_metrics.get('winrate', 0)}% -> "
            f"{best_metrics.get('winrate', 0)}%"
        ),
        (
            "Average PnL: "
            f"{baseline_metrics.get('average_pnl', 0)} -> "
            f"{best_metrics.get('average_pnl', 0)}"
        ),
        (
            "PF: "
            f"{baseline_metrics.get('profit_factor', 0)} -> "
            f"{best_metrics.get('profit_factor', 0)}"
        ),
        (
            "Max DD: "
            f"{baseline_metrics.get('max_drawdown', 0)} -> "
            f"{best_metrics.get('max_drawdown', 0)}"
        ),
    ]

    lines = [
        "🧪 Experiments",
        "",
        f"Отчёт обновлён: {summary_generated_at}",
        *format_experiment_metrics("Лучшая конфигурация", best),
        "",
        *format_experiment_metrics("Худшая конфигурация", worst),
        "",
        *format_experiment_metrics("Baseline", baseline_row),
        "",
        *compare_lines,
    ]
    if full:
        ranked = sorted(
            report.get("results", []),
            key=lambda item: (
                safe_float(item.get("trade_metrics", {}).get("profit_factor")),
                safe_float(item.get("trade_metrics", {}).get("average_pnl")),
                safe_float(item.get("trade_metrics", {}).get("winrate")),
            ),
            reverse=True,
        )[:3]
        lines.extend(["", "Топ-3 сценария:"])
        for index, scenario in enumerate(ranked, start=1):
            metrics = scenario.get("trade_metrics", {})
            lines.append(
                (
                    f"{index}. {scenario.get('scenario', 'N/A')} | "
                    f"WR {metrics.get('winrate', 0)}% | "
                    f"PF {metrics.get('profit_factor', 0)} | "
                    f"PnL {metrics.get('average_pnl', 0)} | "
                    f"DD {metrics.get('max_drawdown', 0)}"
                )
            )
    return "\n".join(lines)


def format_weight_block(title: str, weights: Mapping[str, Any]) -> List[str]:
    """Format one weight block."""
    return [
        title,
        f"Trend: {weights.get('trend', 'N/A')}",
        f"Structure: {weights.get('structure', 'N/A')}",
        f"Momentum: {weights.get('momentum', 'N/A')}",
        f"Risk: {weights.get('risk', 'N/A')}",
        f"Сумма: {weights_sum(weights)}",
        f"Статус: {weights_status(weights)}",
    ]


def describe_weight_changes(
    live_weights: Mapping[str, Any],
    candidate_weights: Mapping[str, Any],
) -> List[str]:
    """Describe changed candidate weights."""
    changes = []
    for key in ("trend", "structure", "momentum", "risk"):
        live_value = live_weights.get(key)
        candidate_value = candidate_weights.get(key)
        if live_value != candidate_value:
            changes.append(f"- {key}: {live_value} -> {candidate_value}")
    return changes or ["- Изменений нет"]


def format_candidate() -> str:
    """Format candidate weights comparison report."""
    experiments_error = ensure_experiments_report()
    if experiments_error:
        return f"🧾 Candidate\n\n{experiments_error}"

    live_weights_payload = read_json(WEIGHTS_FILE)
    candidate_payload = read_json(CANDIDATE_WEIGHTS_FILE)
    if not live_weights_payload:
        return "🧾 Candidate\n\nФайл strategy_weights.json пуст или повреждён."
    if not candidate_payload:
        return "🧾 Candidate\n\nФайл candidate_weights.json пуст или отсутствует."

    experiments_report = read_json(EXPERIMENTS_REPORT_FILE)
    baseline = next(
        (row for row in experiments_report.get("results", []) if row.get("scenario") == "baseline"),
        {},
    )
    best = experiments_report.get("best_scenario", {})
    live_global = live_weights_payload.get("global", {})
    candidate_global = candidate_payload.get("global", {})
    metadata = candidate_payload.get("metadata", {})

    baseline_metrics = baseline.get("trade_metrics", {})
    best_metrics = best.get("trade_metrics", {})

    lines = [
        "🧾 Candidate",
        "",
        *format_weight_block("Боевые веса", live_global),
        "",
        *format_weight_block("Кандидатные веса", candidate_global),
        "",
        "Что изменено:",
        *describe_weight_changes(live_global, candidate_global),
        "",
        "Почему выбран кандидат:",
        metadata.get(
            "selected_because",
            "Кандидат выбран по лучшему результату в experiments-report.",
        ),
        f"Сценарий: {metadata.get('selected_scenario', best.get('scenario', 'N/A'))}",
        "",
        "Baseline vs candidate:",
        f"Winrate: {baseline_metrics.get('winrate', 0)}% -> {best_metrics.get('winrate', 0)}%",
        f"PF: {baseline_metrics.get('profit_factor', 0)} -> {best_metrics.get('profit_factor', 0)}",
        f"Average PnL: {baseline_metrics.get('average_pnl', 0)} -> {best_metrics.get('average_pnl', 0)}",
        f"Max DD: {baseline_metrics.get('max_drawdown', 0)} -> {best_metrics.get('max_drawdown', 0)}",
    ]
    return "\n".join(lines)


def ensure_auto_learning_report() -> Optional[str]:
    """Build auto-learning recommendation if it is missing."""
    if AUTO_LEARNING_FILE.exists() and AUTO_LEARNING_FILE.stat().st_size > 0:
        return None
    if not VENV_PYTHON.exists():
        return (
            "Не найден проектный Python: "
            f"{VENV_PYTHON}. Сначала проверь venv."
        )
    try:
        subprocess.run(
            [str(VENV_PYTHON), str(BASE_DIR / "auto_learning.py")],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        return f"Не удалось построить auto-learning рекомендацию: {details}"
    return None


def format_learn() -> str:
    """Format safe auto-learning recommendation."""
    error = ensure_auto_learning_report()
    if error:
        return f"🧠 Learn\n\n{error}"

    report = read_json(AUTO_LEARNING_FILE)
    if not report:
        return (
            "🧠 Learn\n\n"
            "Файл auto_learning_recommendation.json пуст или повреждён."
        )

    changes = report.get("proposed_changes", [])
    change_lines = [
        f"- {item.get('weight')}: {item.get('from')} -> {item.get('to')}"
        for item in changes
    ] or ["- Пока нет безопасных изменений"]

    why_lines = [
        f"- {item}" for item in report.get("why_proposed", [])[:3]
    ] or ["- Пока нет причин"]
    blocked_lines = [
        f"- {item}" for item in report.get("not_applied_because", [])[:4]
    ] or ["- Ограничений нет"]

    comparison = report.get("baseline_vs_candidate", {})
    baseline = comparison.get("baseline", {})
    candidate = comparison.get("candidate", {})
    candidate_weights = report.get("candidate_weights", {}).get("global", {})

    lines = [
        "🧠 Learn",
        "",
        f"Статус: {report.get('status', 'N/A')}",
        f"Уверенность: {report.get('confidence', 'N/A')}",
        f"Сценарий: {report.get('candidate_selected_scenario', 'N/A')}",
        f"Закрытых сделок: {report.get('closed_trades_count', 0)}",
        f"Сумма candidate-весов: {weights_sum(candidate_weights)}",
        f"Статус candidate-весов: {weights_status(candidate_weights)}",
        "",
        "Предлагаемые изменения:",
        *change_lines,
        "",
        "Почему:",
        *why_lines,
        "",
        "Baseline vs candidate:",
        f"Winrate: {baseline.get('winrate', 0)}% -> {candidate.get('winrate', 0)}%",
        f"PF: {baseline.get('profit_factor', 0)} -> {candidate.get('profit_factor', 0)}",
        f"Average PnL: {baseline.get('average_pnl', 0)} -> {candidate.get('average_pnl', 0)}",
        f"Max DD: {baseline.get('max_drawdown', 0)} -> {candidate.get('max_drawdown', 0)}",
        "",
        "Почему не применено:",
        *blocked_lines,
    ]
    return "\n".join(lines)


def ensure_quality_report() -> Optional[str]:
    """Build data quality report if it is missing."""
    if DATA_QUALITY_FILE.exists() and DATA_QUALITY_FILE.stat().st_size > 0:
        return None
    if not VENV_PYTHON.exists():
        return (
            "Не найден проектный Python: "
            f"{VENV_PYTHON}. Сначала проверь venv."
        )
    try:
        subprocess.run(
            [str(VENV_PYTHON), str(BASE_DIR / "data_quality_check.py")],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        return f"Не удалось построить data-quality отчёт: {details}"
    return None


def format_quality() -> str:
    """Format data quality report for Telegram."""
    error = ensure_quality_report()
    if error:
        return f"🧪 Quality\n\n{error}"

    report = read_json(DATA_QUALITY_FILE)
    if not report:
        return (
            "🧪 Quality\n\n"
            "Файл data_quality_report.json пуст или повреждён."
        )

    issues = report.get("top_issues", [])[:4]
    issue_lines = [
        f"- [{item.get('severity')}] {item.get('message')}"
        for item in issues
    ] or ["- Критичных проблем не найдено"]

    fix_lines = [
        f"- {item}" for item in report.get("what_to_fix", [])[:3]
    ] or ["- Ничего срочного"]

    lines = [
        "🧪 Quality",
        "",
        f"Статус: {report.get('status', 'N/A')}",
        f"Проблем найдено: {report.get('issues_count', 0)}",
        f"ERROR: {report.get('severity_breakdown', {}).get('ERROR', 0)}",
        f"WARNING: {report.get('severity_breakdown', {}).get('WARNING', 0)}",
        "",
        "Главные проблемы:",
        *issue_lines,
        "",
        "Что исправить:",
        *fix_lines,
    ]
    return "\n".join(lines)


def ensure_filters_report() -> Optional[str]:
    """Build filter effectiveness report if it is missing."""
    if FILTERS_REPORT_FILE.exists() and FILTERS_REPORT_FILE.stat().st_size > 0:
        return None
    if not VENV_PYTHON.exists():
        return (
            "Не найден проектный Python: "
            f"{VENV_PYTHON}. Сначала проверь venv."
        )
    try:
        subprocess.run(
            [str(VENV_PYTHON), str(BASE_DIR / "filter_effectiveness.py")],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        return f"Не удалось построить filter-effectiveness отчёт: {details}"
    return None


def format_filters() -> str:
    """Format filter effectiveness summary for Telegram."""
    error = ensure_filters_report()
    if error:
        return f"🧩 Filters\n\n{error}"

    report = read_json(FILTERS_REPORT_FILE)
    if not report:
        return (
            "🧩 Filters\n\n"
            "Файл filter_effectiveness_report.json пуст или повреждён."
        )

    ranking = report.get("ranking", {})
    recommendations = [
        f"- {item}" for item in report.get("recommendations", [])[:3]
    ] or ["- Пока нет рекомендаций"]

    lines = [
        "🧩 Filters",
        "",
        f"Самый полезный: {ranking.get('most_useful', 'N/A')}",
        f"Самый вредный: {ranking.get('most_harmful', 'N/A')}",
        f"Самый строгий: {ranking.get('most_strict', 'N/A')}",
        f"Самый бесполезный: {ranking.get('most_useless', 'N/A')}",
        f"Главный блокирующий фильтр: {ranking.get('main_blocker', 'N/A')}",
        "",
        "Рекомендации:",
        *recommendations,
    ]
    return "\n".join(lines)


def ensure_blocked_report() -> Optional[str]:
    """Build blocked trade simulation report if it is missing."""
    return ensure_blocked_report_for("Momentum")


def blocked_report_file(blocker: str) -> Path:
    return BASE_DIR / f"blocked_trade_simulation_{blocker}_report.json"


def ensure_blocked_report_for(blocker: str) -> Optional[str]:
    """Build blocked trade simulation report for a blocker if it is missing."""
    report_file = blocked_report_file(blocker)
    if report_file.exists() and report_file.stat().st_size > 0:
        return None
    if not VENV_PYTHON.exists():
        return (
            "Не найден проектный Python: "
            f"{VENV_PYTHON}. Сначала проверь venv."
        )
    try:
        subprocess.run(
            [
                str(VENV_PYTHON),
                str(BASE_DIR / "blocked_trade_simulator.py"),
                "--blocker",
                blocker,
            ],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        return f"Не удалось построить blocked-trade simulation: {details}"
    return None


def format_blocked(blocker: str = "Momentum") -> str:
    """Format blocked trade simulation report for Telegram."""
    error = ensure_blocked_report_for(blocker)
    if error:
        return f"🚧 Blocked\n\n{error}"

    report = read_json(blocked_report_file(blocker))
    if not report:
        return (
            "🚧 Blocked\n\n"
            f"Файл blocked_trade_simulation_{blocker}_report.json пуст или повреждён."
        )
    if report.get("status") == "WAITING_FOR_DATA":
        return "\n".join(
            [
                "🚧 Blocked",
                "",
                (
                    f"Найдено {report.get('blocked_momentum_candidates', 0)} кандидатов, "
                    "но нет OHLCV-данных для симуляции."
                ),
                f"Blocker: {blocker}",
                f"Статус: {report.get('status', 'N/A')}",
                f"Файл кандидатов: {Path(report.get('candidates_file', 'blocked_trade_candidates.csv')).name}",
                "",
                "Нужно запустить симулятор в среде с доступом к Bybit",
                "или добавить локальный OHLCV-cache.",
            ]
        )
    if report.get("status") != "OK":
        return (
            "🚧 Blocked\n\n"
            f"Blocker: {blocker}\n"
            f"Статус: {report.get('status', 'N/A')}\n"
            f"Ошибка: {report.get('error', 'N/A')}"
        )

    summary = report.get("summary", {})
    if blocker == "ALL":
        by_blocker = report.get("by_blocker", {})
        blocker_lines = [
            (
                f"- {name}: sim={payload.get('simulated_trades', 0)} | "
                f"WR {payload.get('summary', {}).get('winrate', 0)}% | "
                f"PF {payload.get('summary', {}).get('profit_factor', 0)}"
            )
            for name, payload in by_blocker.items()
        ]
        return "\n".join(
            [
                "🚧 Blocked",
                "",
                f"Blocker: {blocker}",
                f"Симуляций: {report.get('simulated_trades', 0)}",
                f"Winrate: {summary.get('winrate', 0)}%",
                f"PF: {summary.get('profit_factor', 0)}",
                f"Average R: {summary.get('average_r', 0)}",
                "",
                "По blocker:",
                *blocker_lines,
                "",
                f"Рекомендация: {report.get('recommendation', 'N/A')}",
            ]
        )
    lines = [
        "🚧 Blocked",
        "",
        f"Blocker: {blocker}",
        f"Заблокированных сделок: {report.get('blocked_momentum_candidates', 0)}",
        f"Winrate: {summary.get('winrate', 0)}%",
        f"PF: {summary.get('profit_factor', 0)}",
        f"Average R: {summary.get('average_r', 0)}",
        f"Лучший символ: {report.get('best_symbol', 'N/A')}",
        f"Худший символ: {report.get('worst_symbol', 'N/A')}",
        "",
        f"Рекомендация: {report.get('recommendation', 'N/A')}",
    ]
    return "\n".join(lines)


def ensure_regime_report() -> Optional[str]:
    """Build market regime report if it is missing."""
    if MARKET_REGIME_FILE.exists() and MARKET_REGIME_FILE.stat().st_size > 0:
        return None
    if not VENV_PYTHON.exists():
        return (
            "Не найден проектный Python: "
            f"{VENV_PYTHON}. Сначала проверь venv."
        )
    try:
        subprocess.run(
            [str(VENV_PYTHON), str(BASE_DIR / "market_regime.py")],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        return f"Не удалось построить market-regime report: {details}"
    return None


def format_regime() -> str:
    """Format current market regime report for Telegram."""
    error = ensure_regime_report()
    if error:
        return f"🌍 Regime\n\n{error}"

    report = read_json(MARKET_REGIME_FILE)
    if not report:
        return "🌍 Regime\n\nФайл market_regime_report.json пуст или повреждён."

    symbols = report.get("symbols", {})
    if not symbols:
        errors = report.get("errors", {})
        lines = ["🌍 Regime", "", "Не удалось определить режимы по символам."]
        for symbol, message in list(errors.items())[:3]:
            lines.append(f"- {symbol}: {message}")
        return "\n".join(lines)

    lines = ["🌍 Regime", ""]
    for symbol, payload in symbols.items():
        lines.append(f"{symbol}")
        lines.append(f"Режим: {payload.get('primary_regime', 'N/A')}")
        lines.append(f"Волатильность: {payload.get('volatility_regime', 'N/A')}")
        lines.append(f"Momentum: {payload.get('momentum_state', 'N/A')}")
        lines.append("")
    return "\n".join(lines)


def format_status() -> str:
    """Format agent status."""
    stats = read_json(STATS_FILE)
    signals = read_csv_rows(SIGNALS_FILE)
    latest_signal = max(
        (row.get("timestamp", "") for row in signals),
        default="",
    )
    latest_dt = parse_time(latest_signal)
    now = datetime.now(timezone.utc)
    online = False
    if latest_dt:
        online = (now - latest_dt).total_seconds() <= RUN_INTERVAL * 2

    return "\n".join(
        [
            "⚙️ Статус агента",
            f"Агент: {'Онлайн' if online else 'Нет свежего цикла'}",
            f"Цикл: {stats.get('runs', 0)}",
            f"Проанализировано символов: {stats.get('analyzed_symbols', 0)}",
            f"Ошибки API: {stats.get('api_errors', 0)}",
            f"SETUP-сигналы: {stats.get('setup_signals', 0)}",
            f"NO TRADE-сигналы: {stats.get('no_trade_signals', 0)}",
            f"Последний сигнал: {format_time(latest_signal)}",
            f"Время: {now.strftime('%Y-%m-%d %H:%M UTC')}",
        ]
    )


def agent_health_snapshot() -> Dict[str, Any]:
    """Return a compact agent health snapshot without recomputing analytics."""
    stats = read_json(STATS_FILE)
    signals = read_csv_rows(SIGNALS_FILE)
    latest_signal = max((row.get("timestamp", "") for row in signals), default="")
    latest_dt = parse_time(latest_signal)
    now = datetime.now(timezone.utc)
    online = False
    if latest_dt:
        online = (now - latest_dt).total_seconds() <= RUN_INTERVAL * 2
    return {
        "online": online,
        "cycle": stats.get("runs", 0),
        "latest_signal": latest_signal,
    }


def human_age(seconds: float) -> str:
    """Format an age in seconds for compact dashboard output."""
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = int(minutes // 60)
    if hours < 48:
        return f"{hours} h ago"
    days = int(hours // 24)
    return f"{days} d ago"


def report_freshness_snapshot(latest_signal: str) -> Dict[str, Any]:
    """Check whether key reports lag behind the latest signal too much."""
    latest_dt = parse_time(latest_signal)
    reports = {
        "Data Quality": DATA_QUALITY_FILE,
        "Research": RESEARCH_REPORT_FILE,
        "Experiments": EXPERIMENTS_REPORT_FILE,
        "Auto-Learning": AUTO_LEARNING_FILE,
        "Market Regime": MARKET_REGIME_FILE,
        "AI Coach": AI_COACH_REPORT_FILE,
        "PostTrade": POST_TRADE_REPORT_FILE,
    }
    if latest_dt is None:
        return {"status": "N/A", "stale": [], "missing": [], "ages": []}

    stale = []
    missing = []
    ages = []
    now = datetime.now(timezone.utc).timestamp()
    latest_ts = latest_dt.timestamp()
    for name, path in reports.items():
        if not path.exists() or path.stat().st_size == 0:
            missing.append(name)
            ages.append(f"{name}: missing")
            continue
        report_mtime = path.stat().st_mtime
        ages.append(f"{name}: {human_age(max(0, now - report_mtime))}")
        if report_mtime < latest_ts - FRESHNESS_GRACE_SECONDS:
            stale.append(name)

    status = "OK" if not stale and not missing else "STALE"
    return {
        "status": status,
        "stale": stale,
        "missing": missing,
        "ages": ages,
    }


def format_analytics_status() -> str:
    """Build the legacy analytics health snapshot for Developer use."""
    health = agent_health_snapshot()
    trade_stats = calculate_trade_stats()
    freshness = report_freshness_snapshot(health["latest_signal"])

    auto_learning = read_json(AUTO_LEARNING_FILE)
    quality = read_json(DATA_QUALITY_FILE)
    experiments = read_json(EXPERIMENTS_REPORT_FILE)
    candidate_payload = read_json(CANDIDATE_WEIGHTS_FILE)
    regime_report = read_json(MARKET_REGIME_FILE)
    filters_report = read_json(FILTERS_REPORT_FILE)

    auto_status = auto_learning.get("status", "N/A")
    quality_status = quality.get("status", "N/A")
    best_experiment = experiments.get("best_scenario", {}).get("scenario", "N/A")
    candidate_global = candidate_payload.get("global", {})
    candidate_status = (
        weights_status(candidate_global) if candidate_global else "N/A"
    )
    filter_ranking = filters_report.get("ranking", {})
    main_blocker = filter_ranking.get("main_blocker", "N/A")

    regimes = []
    for payload in regime_report.get("symbols", {}).values():
        regime = payload.get("primary_regime")
        if regime and regime not in regimes:
            regimes.append(regime)
    if not regimes:
        current_regime = "N/A"
    elif len(regimes) == 1:
        current_regime = regimes[0]
    else:
        current_regime = ", ".join(regimes[:3])

    max_drawdown = trade_stats.get("max_drawdown_r", 0)

    next_action = "Проверить актуальность отчётов."
    if freshness["status"] == "STALE":
        next_action = "Обновить устаревшие аналитические отчёты."
    elif quality_status == "ERROR":
        next_action = "Исправить критические проблемы Data Quality."
    elif quality_status == "WARNING":
        next_action = "Разобрать предупреждения Data Quality."
    elif trade_stats["closed"] < 30:
        next_action = f"Собрать ещё {30 - trade_stats['closed']} закрытых сделок."
    elif auto_status in {"NOT_ENOUGH_DATA", "BLOCKED", "REJECTED_BY_RULES"}:
        next_action = "Продолжать наблюдение и не применять candidate вручную."
    elif candidate_status != "VALID":
        next_action = "Проверить candidate weights перед следующими тестами."
    elif best_experiment != "N/A":
        next_action = f"Подтвердить {best_experiment} на extended backtest."

    stale_reports = freshness["stale"] + [
        f"{name} missing" for name in freshness["missing"]
    ]
    stale_line = ", ".join(stale_reports[:5]) if stale_reports else "нет"
    age_lines = freshness.get("ages", [])[:4]

    lines = [
            "📊 Dashboard",
            "",
            f"🟢 Статус агента: {'Online' if health['online'] else 'Offline'}",
            f"📈 Последний цикл: {health['cycle']}",
            f"🕒 Freshness: {freshness['status']}",
            f"📄 Устарели: {stale_line}",
            f"⏱ Возраст: {' | '.join(age_lines) if age_lines else 'N/A'}",
            f"📊 Закрытых сделок: {trade_stats['closed']}",
            f"🧠 Auto-Learning: {auto_status}",
            f"🛡 Data Quality: {quality_status}",
            f"🧪 Лучшая experiment-конфигурация: {best_experiment}",
            f"🧾 Candidate: {candidate_status}",
            f"🌍 Текущий режим рынка: {current_regime}",
            f"🚧 Главный blocker: {main_blocker}",
            f"📈 Winrate: {trade_stats['win_rate']:.1f}%",
            f"💰 Profit Factor: {trade_stats['profit_factor']:.2f}",
            f"📉 Max Drawdown: {max_drawdown} R",
            "",
            f"🎯 Следующее действие: {next_action}",
    ]
    if len(stale_reports) > 5:
        lines.insert(5, f"📄 Ещё устарели: {len(stale_reports) - 5}")
    return "\n".join(lines)


def ensure_coach_report() -> Optional[str]:
    """Build AI Coach report if it is missing or stale."""
    sources = [
        STATS_FILE,
        TRADES_FILE,
        FILTERS_REPORT_FILE,
        POST_TRADE_REPORT_FILE,
        MARKET_REGIME_FILE,
        AUTO_LEARNING_FILE,
        EXPERIMENTS_REPORT_FILE,
        DATA_QUALITY_FILE,
        BASE_DIR / "blocked_trade_simulation_ALL_report.json",
    ]
    files_ready = (
        AI_COACH_REPORT_FILE.exists()
        and AI_COACH_REPORT_FILE.stat().st_size > 0
        and AI_COACH_SUMMARY_FILE.exists()
        and AI_COACH_SUMMARY_FILE.stat().st_size > 0
    )
    latest_source_mtime = max(
        (path.stat().st_mtime for path in sources if path.exists()),
        default=0,
    )
    stale = (
        files_ready
        and latest_source_mtime > AI_COACH_REPORT_FILE.stat().st_mtime
    )
    if files_ready and not stale:
        return None
    if not VENV_PYTHON.exists():
        return (
            "Не найден проектный Python: "
            f"{VENV_PYTHON}. Сначала проверь venv."
        )
    try:
        subprocess.run(
            [str(VENV_PYTHON), str(BASE_DIR / "ai_coach.py")],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        return f"Не удалось построить AI Coach отчёт: {details}"
    return None


def format_coach() -> str:
    """Format AI Coach output for Telegram."""
    error = ensure_coach_report()
    if error:
        return f"🧠 AI Coach\n\n{error}"
    if AI_COACH_SUMMARY_FILE.exists() and AI_COACH_SUMMARY_FILE.stat().st_size > 0:
        return AI_COACH_SUMMARY_FILE.read_text(encoding="utf-8")
    report = read_json(AI_COACH_REPORT_FILE)
    if not report:
        return "🧠 AI Coach\n\nФайл ai_coach_report.json пуст или повреждён."
    actions = [f"- {item}" for item in report.get("actions", [])]
    return "\n".join(
        [
            "🧠 AI Coach",
            "",
            f"Сегодня рынок: {report.get('market_regime', 'N/A')}",
            f"Главный blocker: {report.get('main_blocker', 'N/A')}",
            f"Главная проблема: {report.get('top_loss_reason', 'N/A')}",
            "",
            "Что делать сегодня:",
            *actions,
        ]
    )


def format_market() -> str:
    """Format latest market decisions per symbol."""
    latest = latest_by_symbol(read_csv_rows(SIGNALS_FILE))
    diagnostics = latest_by_symbol(read_csv_rows(DIAGNOSTICS_FILE))
    if not latest:
        return "📈 Рынок\n\nДанные по сигналам пока отсутствуют."

    lines = ["📈 Рынок", ""]
    for symbol in sorted(latest):
        signal = latest[symbol]
        diagnostic = diagnostics.get(symbol, {})
        blocker = diagnostic.get("primary_blocker", "N/A")
        lines.append(f"{symbol}")
        lines.append(f"Сигнал: {signal.get('signal', 'N/A')}")
        lines.append(f"Score: {signal.get('score', 'N/A')}")
        lines.append(f"Confidence: {signal.get('confidence', 'N/A')}")
        lines.append(f"Блокер: {blocker}")
        lines.append("")
    return "\n".join(lines)


def format_watchlist() -> str:
    """Format symbols that are actionable or near SETUP."""
    latest = latest_by_symbol(read_csv_rows(SIGNALS_FILE))
    diagnostics = latest_by_symbol(read_csv_rows(DIAGNOSTICS_FILE))
    rows = []

    for symbol, signal in latest.items():
        diagnostic = diagnostics.get(symbol, {})
        potential = safe_float(diagnostic.get("potential_score"))
        signal_name = signal.get("signal", "N/A")
        if signal_name in {"HIGH PRIORITY", "SETUP", "WATCH", "WAIT"}:
            rows.append((symbol, signal_name, potential))
        elif potential >= 23:
            rows.append((symbol, "Почти SETUP", potential))

    if not rows:
        return "📋 Watchlist\n\nНет активных или близких к SETUP символов."

    rows.sort(key=lambda item: item[2], reverse=True)
    lines = ["📋 Watchlist", ""]
    for symbol, signal_name, potential in rows:
        lines.append(f"{symbol}")
        lines.append(f"Статус: {signal_name}")
        lines.append(f"Potential: {potential:g}")
        lines.append("")
    return "\n".join(lines)


def latest_debug_for_symbol(symbol: str) -> Optional[Dict[str, str]]:
    """Find the latest decision_debug row for a symbol."""
    target = normalize_symbol(symbol)
    rows = [
        row
        for row in read_csv_rows(DECISION_DEBUG_FILE)
        if row.get("symbol", "").upper() == target
    ]
    if not rows:
        return None
    return max(rows, key=lambda row: row.get("timestamp", ""))


def format_diagnostics(symbol: str) -> str:
    """Recreate the terminal diagnostics report for a symbol."""
    row = latest_debug_for_symbol(symbol)
    if row is None:
        return (
            "🧠 Диагностика\n\n"
            f"Нет данных decision_debug для {symbol.upper()}."
        )

    decision = SimpleNamespace(
        direction=row.get("direction", "NEUTRAL"),
        signal=row.get("signal", "UNKNOWN"),
        score=safe_float(row.get("score")),
        long_total=safe_float(row.get("long_total")),
        short_total=safe_float(row.get("short_total")),
    )
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

    diagnostics = DecisionDiagnostics(
        symbol=row.get("symbol", ""),
        weights=load_weights(row.get("symbol", "")),
        auto_log=False,
    )
    report = diagnostics.analyze(decision, trend, structure, momentum, risk)
    return localize_diagnostics(diagnostics.format_report(report))


def calculate_trade_stats() -> Dict[str, Any]:
    """Calculate trade stats from one shared direction-aware R method."""
    rows = read_csv_rows(TRADES_FILE)
    metrics = aggregate_trade_metrics(rows)
    open_trades = [row for row in rows if row.get("status") == "OPEN"]

    rr_values = []
    for row in rows:
        entry = safe_float(row.get("entry"))
        stop_loss = safe_float(row.get("stop_loss"))
        take_profit = safe_float(row.get("take_profit"))
        risk = abs(entry - stop_loss)
        reward = abs(take_profit - entry)
        if risk:
            rr_values.append(reward / risk)

    avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0.0
    return {
        "total": len(rows),
        "closed": metrics["closed_trades"],
        "metrics_trades": metrics["metrics_trades"],
        "incomplete_metrics": metrics["incomplete_metrics"],
        "open": len(open_trades),
        "wins": metrics["wins"],
        "losses": metrics["losses"],
        "win_rate": metrics["winrate"],
        "profit_factor": metrics["profit_factor"],
        "net_r": metrics["net_r"],
        "max_drawdown_r": metrics["max_drawdown_r"],
        "avg_rr": avg_rr,
    }


def closed_trade_rows() -> List[Dict[str, str]]:
    """Return closed trades from trades.csv in chronological order."""
    rows = [
        row for row in read_csv_rows(TRADES_FILE)
        if is_closed_trade(row)
    ]
    return sorted(rows, key=lambda row: row.get("closed_at") or row.get("opened_at", ""))


def trade_metrics(rows: Iterable[Mapping[str, str]]) -> Dict[str, Any]:
    """Calculate compact metrics in R; aliases keep old readers working."""
    metrics = aggregate_trade_metrics(rows)
    return {
        "closed_trades": metrics["closed_trades"],
        "trades": metrics["metrics_trades"],
        "incomplete_metrics": metrics["incomplete_metrics"],
        "wins": metrics["wins"],
        "losses": metrics["losses"],
        "winrate": metrics["winrate"],
        "profit_factor": metrics["profit_factor"],
        "average_r": metrics["average_r"],
        "net_r": metrics["net_r"],
        "max_drawdown_r": metrics["max_drawdown_r"],
        "average_pnl": metrics["average_r"],
        "pnl": metrics["net_r"],
    }


def max_drawdown_from_pnls(pnls: Iterable[float]) -> float:
    """Calculate max drawdown from a PnL sequence."""
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    return round(max_drawdown, 2)


def best_worst_streaks(rows: Iterable[Mapping[str, str]]) -> Dict[str, int]:
    """Return best WIN streak and worst LOSS streak."""
    best_win = 0
    worst_loss = 0
    current_win = 0
    current_loss = 0
    for row in rows:
        result = row.get("status") or row.get("result")
        if result == "WIN":
            current_win += 1
            current_loss = 0
        elif result == "LOSS":
            current_loss += 1
            current_win = 0
        best_win = max(best_win, current_win)
        worst_loss = max(worst_loss, current_loss)
    return {"best_win": best_win, "worst_loss": worst_loss}


def format_v2_readiness() -> str:
    """Format Strategy v2 readiness from existing reports."""
    trades = closed_trade_rows()
    trade_stats = trade_metrics(trades)
    quality = read_json(DATA_QUALITY_FILE)
    experiments = read_json(EXPERIMENTS_REPORT_FILE)
    auto_learning = read_json(AUTO_LEARNING_FILE)
    candidate = read_json(CANDIDATE_WEIGHTS_FILE)
    blocked = read_json(BASE_DIR / "blocked_trade_simulation_ALL_report.json")
    freshness = report_freshness_snapshot(agent_health_snapshot()["latest_signal"])

    checks = {
        "Trade sample": trade_stats["trades"] >= 30,
        "Data Quality": quality.get("status") == "OK",
        "Candidate": weights_status(candidate.get("global", {})) == "VALID",
        "Experiments": bool(experiments.get("best_scenario")),
        "Freshness": freshness.get("status") == "OK",
        "Auto Learning": auto_learning.get("status") == "PENDING_APPROVAL",
        "Blocked Analysis": blocked.get("status") in {"OK", "WAITING_FOR_DATA"},
    }
    progress = round(sum(1 for ok in checks.values() if ok) / len(checks) * 100)
    baseline_pf = 0.0
    for row in experiments.get("results", []):
        if row.get("scenario") == "baseline":
            baseline_pf = safe_float(row.get("trade_metrics", {}).get("profit_factor"))
            break
    todo = []
    if trade_stats["trades"] < 30:
        todo.append(f"ещё {30 - trade_stats['trades']} закрытых сделок")
    if baseline_pf <= 0.50:
        todo.append("Profit Factor > 0.50")
    if quality.get("status") != "OK":
        todo.append("Data Quality должен быть OK")
    if freshness.get("status") != "OK":
        todo.append("обновить устаревшие отчёты")
    if auto_learning.get("status") != "PENDING_APPROVAL":
        todo.append("Auto-Learning должен дойти до PENDING_APPROVAL")
    if not todo:
        todo.append("ручное approval перед любыми v2 изменениями")

    lines = [
        "🚀 V2 Readiness",
        f"Progress: {progress}%",
        "",
        f"Closed trades: {trade_stats['trades']} / 30",
    ]
    for name, ok in checks.items():
        lines.append(f"{name}: {'✅' if ok else '❌'}")
    lines.extend(["", "До готовности:"])
    lines.extend(f"• {item}" for item in todo[:5])
    return "\n".join(lines)


def format_symbols_report() -> str:
    """Format symbol ranking from closed trades."""
    trades = closed_trade_rows()
    if not trades:
        return "🏆 Symbols\n\nЗакрытых сделок пока нет."
    by_symbol: Dict[str, List[Dict[str, str]]] = {}
    for row in trades:
        by_symbol.setdefault(row.get("symbol", "N/A"), []).append(row)
    ranked = sorted(
        (
            (symbol, trade_metrics(rows))
            for symbol, rows in by_symbol.items()
        ),
        key=lambda item: (item[1]["net_r"], item[1]["profit_factor"], item[1]["winrate"]),
        reverse=True,
    )
    best = ranked[0][0]
    worst = ranked[-1][0]
    lines = ["🏆 Symbols", "", f"Лучший символ: {best}", f"Худший символ: {worst}", ""]
    for symbol, metrics in ranked:
        lines.append(
            f"{symbol}: WR {metrics['winrate']}% | PF {metrics['profit_factor']} | "
            f"trades {metrics['trades']} | avg R {metrics['average_r']} | "
            f"Net R {metrics['net_r']}"
        )
    return "\n".join(lines)


def format_equity_report() -> str:
    """Format performance in R without inventing a percent ROI or balance."""
    trades = closed_trade_rows()
    if not trades:
        return "💰 Equity\n\nЗакрытых сделок пока нет."
    metrics = aggregate_trade_metrics(trades)
    normalized = [
        row for row in normalize_closed_trades(trades)
        if row.get("metrics_status") == "COMPLETE"
    ]
    streaks = best_worst_streaks(trades)
    by_day: Dict[str, float] = {}
    for row in normalized:
        closed_at = parse_time(row.get("closed_at", ""))
        day = closed_at.date().isoformat() if closed_at else "N/A"
        by_day[day] = by_day.get(day, 0.0) + safe_float(row.get("pnl_r"))
    best_day = max(by_day, key=by_day.get) if by_day else "N/A"
    worst_day = min(by_day, key=by_day.get) if by_day else "N/A"
    return "\n".join(
        [
            "💰 Equity",
            "",
            f"Net R: {metrics['net_r']}",
            f"Max Drawdown: {metrics['max_drawdown_r']} R",
            f"Incomplete metrics: {metrics['incomplete_metrics']}",
            "Баланс и процентный ROI: недоступны без position size.",
            f"Лучшая серия побед: {streaks['best_win']}",
            f"Худшая серия поражений: {streaks['worst_loss']}",
            f"Лучший день: {best_day} ({round(by_day.get(best_day, 0), 2)} R)",
            f"Худший день: {worst_day} ({round(by_day.get(worst_day, 0), 2)} R)",
        ]
    )


def format_timeline_report() -> str:
    """Format strategy evolution timeline from existing trade/report data."""
    trades = closed_trade_rows()
    experiments = read_json(EXPERIMENTS_REPORT_FILE)
    baseline = {}
    for row in experiments.get("results", []):
        if row.get("scenario") == "baseline":
            baseline = row.get("trade_metrics", {})
            break
    if not trades:
        return "🕘 Timeline\n\nЗакрытых сделок пока нет."

    first_window = trades[: min(5, len(trades))]
    current = trade_metrics(trades)
    early = trade_metrics(first_window)
    first_date = parse_time(trades[0].get("closed_at", "")) or parse_time(trades[0].get("opened_at", ""))
    last_date = parse_time(trades[-1].get("closed_at", "")) or parse_time(trades[-1].get("opened_at", ""))
    lines = [
        "🕘 Timeline",
        "",
        f"Период: {first_date.date().isoformat() if first_date else 'N/A'} -> {last_date.date().isoformat() if last_date else 'N/A'}",
        f"Сделки: {early['trades']} -> {current['trades']}",
        f"Winrate: {early['winrate']}% -> {current['winrate']}%",
        f"Profit Factor: {early['profit_factor']} -> {current['profit_factor']}",
        f"Net R: {early['net_r']} -> {current['net_r']}",
        f"Max Drawdown: {early['max_drawdown_r']} R -> "
        f"{current['max_drawdown_r']} R",
    ]
    if baseline:
        lines.extend(["", "Experiments baseline:"])
        if "net_r" in baseline and "max_drawdown_r" in baseline:
            lines.extend(
                [
                    f"WR: {baseline.get('winrate', 0)}%",
                    f"PF: {baseline.get('profit_factor', 0)}",
                    f"Net R: {baseline.get('net_r', 0)}",
                    f"Max DD: {baseline.get('max_drawdown_r', 0)} R",
                ]
            )
        else:
            lines.append("Legacy units: метрики не показаны до пересчёта в R.")
    return "\n".join(lines)


def format_stats() -> str:
    """Format trading performance."""
    stats = calculate_trade_stats()
    return "\n".join(
        [
            "📊 Статистика",
            f"Всего сделок: {stats['total']}",
            f"Закрыто: {stats['closed']}",
            f"Открыто: {stats['open']}",
            f"Побед: {stats['wins']}",
            f"Поражений: {stats['losses']}",
            f"Win Rate: {stats['win_rate']:.1f}%",
            f"PF: {stats['profit_factor']:.2f}",
            f"Средний RR: 1:{stats['avg_rr']:.2f}",
        ]
    )


def format_trades() -> str:
    """Format open trades."""
    rows = [row for row in read_csv_rows(TRADES_FILE) if row.get("status") == "OPEN"]
    if not rows:
        return "📂 Сделки\n\nОткрытых сделок нет."

    lines = ["📂 Сделки", ""]
    for row in rows:
        lines.append(
            "\n".join(
                [
                    f"{row.get('symbol')} {row.get('direction')}",
                    f"Вход: {row.get('entry')}",
                    f"SL: {row.get('stop_loss')}",
                    f"TP: {row.get('take_profit')}",
                    f"Открыта: {format_time(row.get('opened_at', ''))}",
                ]
            )
        )
    return "\n\n".join(lines)


def ensure_post_trade_report() -> Optional[str]:
    """Build post-trade analysis artifacts on demand."""
    files_ready = all(
        path.exists() and path.stat().st_size > 0
        for path in (
            POST_TRADE_REPORT_FILE,
            POST_TRADE_SUMMARY_FILE,
            POST_TRADE_TRADES_FILE,
        )
    )
    trades_newer = (
        files_ready
        and TRADES_FILE.exists()
        and TRADES_FILE.stat().st_mtime > POST_TRADE_REPORT_FILE.stat().st_mtime
    )
    if files_ready and not trades_newer:
        return None
    if not VENV_PYTHON.exists():
        return (
            "Не найден проектный Python: "
            f"{VENV_PYTHON}. Сначала проверь venv."
        )
    try:
        subprocess.run(
            [str(VENV_PYTHON), str(BASE_DIR / "post_trade_analysis.py")],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        return f"Не удалось построить post-trade analysis: {details}"
    return None


def format_top_items(items: Mapping[str, Any], limit: int = 3) -> List[str]:
    """Format top counters from report dictionaries."""
    if not items:
        return ["- Пока нет данных"]
    sorted_items = sorted(
        items.items(),
        key=lambda item: safe_float(item[1]),
        reverse=True,
    )
    return [f"- {name}: {count}" for name, count in sorted_items[:limit]]


def format_posttrade() -> str:
    """Format post-trade analysis report for Telegram."""
    error = ensure_post_trade_report()
    if error:
        return f"🔎 PostTrade\n\n{error}"

    report = read_json(POST_TRADE_REPORT_FILE)
    if not report:
        return (
            "🔎 PostTrade\n\n"
            "Файл post_trade_analysis_report.json пуст или повреждён."
        )

    loss_lines = format_top_items(report.get("top_loss_reasons", {}))
    win_lines = format_top_items(report.get("top_win_factors", {}))
    return "\n".join(
        [
            "🔎 PostTrade",
            "",
            f"Закрытых сделок: {report.get('closed_trades', 0)}",
            f"WIN: {report.get('wins', 0)}",
            f"LOSS: {report.get('losses', 0)}",
            f"Winrate: {report.get('winrate', 0)}%",
            "",
            "Топ причин LOSS:",
            *loss_lines,
            "",
            "Топ факторов WIN:",
            *win_lines,
            "",
            f"Лучший символ: {report.get('best_symbol', 'N/A')}",
            f"Худший символ: {report.get('worst_symbol', 'N/A')}",
            "",
            f"Рекомендация: {report.get('recommendation', 'N/A')}",
        ]
    )


def format_calibration() -> str:
    """Format calibration recommendations."""
    report = build_calibration_report(DIAGNOSTICS_FILE)
    save_report(report, CALIBRATION_FILE)
    if not report:
        return "🛠 Калибровка\n\nФайл calibration_report.json пока не создан."

    lines = [
        "🛠 Калибровка",
        f"Статус: {report.get('status', 'N/A')}",
        f"Решений: {report.get('total_decisions', 0)}",
        f"NO TRADE: {report.get('no_trade_decisions', 0)}",
        f"Средний lost score: {report.get('average_lost_score', 0)}",
        "Главные блокеры:",
    ]
    for blocker, count in report.get("primary_blockers", {}).items():
        lines.append(f"{blocker}: {count}")
    lines.append("Рекомендации:")
    for item in report.get("recommendations", []):
        lines.append(f"- {localize_calibration_item(item)}")
    return "\n".join(lines)


def ensure_research_report() -> Optional[str]:
    """Build strategy research artifacts on demand if they are missing."""
    report_exists = (
        RESEARCH_REPORT_FILE.exists()
        and RESEARCH_REPORT_FILE.stat().st_size > 0
    )
    summary_exists = (
        RESEARCH_SUMMARY_FILE.exists()
        and RESEARCH_SUMMARY_FILE.stat().st_size > 0
    )
    if report_exists and summary_exists:
        return None
    if not VENV_PYTHON.exists():
        return (
            "Не найден проектный Python: "
            f"{VENV_PYTHON}. Сначала проверь venv."
        )
    try:
        subprocess.run(
            [str(VENV_PYTHON), str(BASE_DIR / "strategy_research.py")],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        return f"Не удалось построить research-отчёт: {details}"
    return None


def format_research(section: str = "overview") -> str:
    """Read and format the saved orchestrator report without recomputation."""
    report = read_json(RESEARCH_ORCHESTRATOR_REPORT_FILE)
    return format_research_orchestrator(
        report,
        section=section,
        query=section,
    )


def format_history(limit: int = 10) -> str:
    """Format latest signal history."""
    rows = sorted(
        read_csv_rows(SIGNALS_FILE),
        key=lambda row: row.get("timestamp", ""),
        reverse=True,
    )
    if not rows:
        return "🕘 История\n\nИстория сигналов пока пуста."

    lines = ["🕘 История", ""]
    for row in rows[:limit]:
        lines.append(format_time(row.get("timestamp", "")))
        lines.append(f"{row.get('symbol', '')} | {row.get('signal', '')}")
        lines.append(
            f"Score: {row.get('score', '')} | "
            f"Confidence: {row.get('confidence', '')}"
        )
        lines.append("")
    return "\n".join(lines)


def format_daily_report() -> str:
    """Format a compact daily report."""
    today = datetime.now(timezone.utc).date()
    signals = [
        row
        for row in read_csv_rows(SIGNALS_FILE)
        if (parse_time(row.get("timestamp", "")) or datetime.min.replace(
            tzinfo=timezone.utc
        )).date() == today
    ]
    diagnostics = [
        row
        for row in read_csv_rows(DIAGNOSTICS_FILE)
        if (parse_time(row.get("timestamp", "")) or datetime.min.replace(
            tzinfo=timezone.utc
        )).date() == today
    ]
    signal_counts: Dict[str, int] = {}
    blockers: Dict[str, int] = {}
    for row in signals:
        signal = row.get("signal", "UNKNOWN")
        signal_counts[signal] = signal_counts.get(signal, 0) + 1
    for row in diagnostics:
        blocker = row.get("primary_blocker", "UNKNOWN")
        blockers[blocker] = blockers.get(blocker, 0) + 1

    lines = [
        "🧾 Дневной отчёт",
        f"Дата: {today.isoformat()} UTC",
        f"Сигналов обработано: {len(signals)}",
        "Распределение сигналов:",
    ]
    for signal, count in sorted(signal_counts.items()):
        lines.append(f"{signal}: {count}")
    lines.append("Главные блокеры:")
    sorted_blockers = sorted(
        blockers.items(),
        key=lambda item: item[1],
        reverse=True,
    )
    for blocker, count in sorted_blockers:
        lines.append(f"{blocker}: {count}")
    lines.append("")
    lines.append(format_stats())
    return "\n".join(lines)


def format_news(section: str = "overview") -> str:
    """Format a ready News Observer artifact without network or subprocesses."""
    report = read_json(MARKET_NEWS_FILE)
    if not report:
        return (
            "📰 Новости рынка\n\n"
            "Готовый новостной feed пока отсутствует или повреждён.\n"
            "News Observer обновит его в следующем цикле."
        )
    sources = read_json(MARKET_NEWS_SOURCES_FILE)
    health = read_json(MARKET_NEWS_HEALTH_FILE)
    return format_news_v2(
        report,
        section=(section or "overview").strip(),
        health=health,
        sources=sources,
    )


def format_heatmap() -> str:
    """Format market heatmap output for Telegram."""
    error = run_readonly_module("market_heatmap.py")
    if error:
        return f"🗺 Heatmap\n\n{error}"
    report = read_json(MARKET_HEATMAP_FILE)
    if not report:
        return "🗺 Heatmap\n\nФайл market_heatmap_report.json пуст или повреждён."

    summary = report.get("summary", {})
    lines = [
        "🗺 Heatmap",
        "",
        "Heatmap Summary",
        f"HIGH PRIORITY: {summary.get('HIGH PRIORITY', 0)}",
        f"SETUP: {summary.get('SETUP', 0)}",
        f"WATCH: {summary.get('WATCH', 0)}",
        f"NEAR SETUP: {summary.get('NEAR SETUP', 0)}",
        f"NO TRADE: {summary.get('NO TRADE', 0)}",
        f"Bullish News: {summary.get('Bullish News', 0)}",
        f"Bearish News: {summary.get('Bearish News', 0)}",
        f"Neutral News: {summary.get('Neutral News', 0)}",
        "",
    ]
    diagnostics_rows = read_csv_rows(DIAGNOSTICS_FILE)
    explanation_rows = read_csv_rows(EXPLANATIONS_FILE)
    for row in report.get("symbols", [])[:12]:
        lines.append(
            f"{str(row.get('symbol', '')).replace('/USDT', '')} {row.get('overall', '⚪')} "
            f"Trend {row.get('trend', '⚪')} "
            f"Momentum {row.get('momentum', '⚪')} "
            f"Volume {row.get('volume', '⚪')} "
            f"News {row.get('news', '⚪')}"
        )
        lines.append(
            f"Confidence {row.get('confidence', 0)} | "
            f"Edge {row.get('edge', 0)} | "
            f"{row.get('decision', 'N/A')}"
        )
        failed_filters = v5_failed_filters_for(
            str(row.get("symbol", "")),
            diagnostics_rows=diagnostics_rows,
            explanation_rows=explanation_rows,
        )
        reason = (
            f"Не хватает: {' + '.join(failed_filters)}"
            if failed_filters
            else str(row.get("reason", "N/A"))
        )
        lines.append(f"Причина: {reason}")
    lines.append("")
    lines.append("Heatmap только показывает контекст рынка.")
    return "\n".join(lines)


def format_intelligence() -> str:
    """Run Market Intelligence Hub and return its text summary."""
    error = run_readonly_module("market_intelligence_hub.py", timeout=120)
    if error:
        brief_error = " ".join(error.split())[:600]
        return f"Ошибка Market Intelligence: {brief_error}"

    try:
        text = MARKET_INTELLIGENCE_SUMMARY_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        brief_error = " ".join(str(exc).split())[:600]
        return f"Ошибка Market Intelligence: {brief_error}"

    if not text:
        return "Market Intelligence пока недоступен. Попробуй позже."

    text = inject_news_freshness_into_summary(text)
    text = inject_live_monitor_into_text(text)
    if len(text) > MAX_MESSAGE_LENGTH:
        return text[:MAX_MESSAGE_LENGTH - 3].rstrip() + "..."
    return text


def live_monitor_snippet() -> str:
    """Return compact Live Monitor status for shared Telegram screens."""
    state = load_live_monitor_state()
    if not state:
        return "Live Monitor:\n🔴 OFFLINE\nЦены обновлены:\nнет данных"
    status = str(state.get("status", "OFFLINE"))
    emoji = {
        "ONLINE": "🟢",
        "IDLE": "🟡",
        "DEGRADED": "🟡",
        "OFFLINE": "🔴",
    }.get(status, "⚪")
    return "\n".join(
        [
            "Live Monitor:",
            f"{emoji} {status}",
            "Цены обновлены:",
            live_state_age_text(state),
        ]
    )


def inject_live_monitor_into_text(text: str) -> str:
    """Append Live Monitor status to an existing text response."""
    snippet = live_monitor_snippet()
    if "Live Monitor:" in text:
        return text
    return text.rstrip() + "\n\n" + snippet


def format_memory(symbol: str = "") -> str:
    """Format trade memory for a symbol or current best setup."""
    args = [symbol.upper()] if symbol else []
    error = run_readonly_module("trade_memory.py", *args)
    if error:
        return f"🧾 Trade Memory\n\n{error}"
    if TRADE_MEMORY_SUMMARY_FILE.exists():
        text = TRADE_MEMORY_SUMMARY_FILE.read_text(encoding="utf-8").strip()
        if text:
            return "🧾 Trade Memory\n\n" + text
    return "🧾 Trade Memory\n\nПохожих ситуаций пока нет."


def format_context() -> str:
    """Format current market context; always return a useful response."""
    error = run_readonly_module("market_intelligence_hub.py")
    if error:
        return f"🔎 Контекст рынка\n\n{error}\n\nКонтекст пока не накоплен."

    report = read_json(MARKET_INTELLIGENCE_FILE)
    market = report.get("market", {}) if report else {}
    signals = report.get("signals", {}) if report else {}
    trades = report.get("trades", {}) if report else {}
    news_impact = report.get("news_impact", {}) if report else {}
    best_signal = next(iter(signals.get("best_signals", []) or []), "нет данных")
    top_risk = next(iter(news_impact.get("risk_rows", []) or []), {})
    stats = calculate_trade_stats()
    loss_rows = [
        row for row in read_csv_rows(TRADES_FILE)
        if row.get("status") == "LOSS" or row.get("result") == "LOSS"
    ]
    last_loss = loss_rows[-1] if loss_rows else {}
    last_loss_label = (
        f"{last_loss.get('symbol')} {last_loss.get('direction', '')}".strip()
        if last_loss else "нет данных"
    )
    context_lines = [
        "🔎 Контекст рынка",
        "",
        f"Режим: {market.get('regime', 'Недостаточно данных')}",
        f"Новости: {market.get('news_sentiment', 'Neutral')}",
        f"Fear & Greed: {market.get('fear_greed') or 'нет данных'}",
        f"Лучший сигнал: {best_signal}",
        f"Открытые сделки: {stats.get('open', 0)}",
        "",
        live_monitor_snippet(),
        "",
        f"Последний LOSS: {last_loss_label}",
        f"Главный риск: {top_risk.get('status', 'Momentum FAIL')}",
        f"Причина LOSS: {trades.get('last_loss_primary', 'нет данных')}",
        "Рекомендация:",
        str(report.get("recommendation", "Ждать подтверждения.") if report else "Ждать подтверждения."),
        "",
        "Shadow News Advisor:",
        str(news_impact.get("shadow_advisor", "Нет данных")),
    ]

    rows = read_csv_rows(TRADE_MARKET_CONTEXT_FILE)
    if not rows:
        context_lines.extend(["", "Контекст сделок пока не накоплен."])
        return "\n".join(context_lines)

    context_lines.extend(["", "Последний контекст сделок:"])
    for row in rows[-5:]:
        context_lines.append(
            f"{row.get('symbol', 'N/A')} {row.get('direction', '')} "
            f"{row.get('result') or row.get('status') or 'N/A'}"
        )
        context_lines.append(
            f"Score {row.get('score', 'N/A')} | "
            f"Confidence {row.get('confidence', 'N/A')} | "
            f"Edge {row.get('directional_edge', 'N/A')}"
        )
        context_lines.append(
            f"Momentum {row.get('momentum', 'N/A')} | "
            f"Trend {row.get('trend', 'N/A')} | "
            f"News {row.get('news_sentiment', 'Neutral')}"
        )
        if row.get("context_notes"):
            context_lines.append(f"Контекст: {row.get('context_notes')}")
        context_lines.append("────────────")
    context_lines.append("Контекст не влияет на открытие/закрытие сделок.")
    return "\n".join(context_lines).rstrip("────────────").rstrip()


def format_lab(args: List[str]) -> str:
    """Run Strategy Lab and format Telegram output."""
    command = args[0].lower() if args else ""
    hypothesis_commands = {
        "hypotheses",
        "cooldown",
        "trend",
        "momentum",
        "atr",
        "news",
        "duplicate",
        "volatility",
        "edge",
    }
    if command in hypothesis_commands:
        runner_args = [] if command == "hypotheses" else [command]
        error = run_readonly_module(
            "strategy_lab/hypothesis_runner.py",
            *runner_args,
            timeout=120,
        )
        if error:
            return f"Ошибка Strategy Lab v2: {' '.join(error.split())[:600]}"
        if command == "hypotheses":
            try:
                text = HYPOTHESIS_SUMMARY_FILE.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            return text or "Strategy Lab v2 пока недоступен. Попробуй позже."

        report = read_json(HYPOTHESIS_REPORT_FILE)
        metrics = [
            row for row in report.get("metrics", [])
            if row.get("group") == command and row.get("hypothesis") != "baseline"
        ]
        if not metrics:
            return "Strategy Lab v2 пока не нашёл данные по этой гипотезе."
        lines = [
            "🧪 Strategy Lab v2",
            "",
            f"Гипотеза: {command}",
            "Shadow Research",
            "Live-стратегия не меняется.",
            "",
        ]
        for row in metrics[:8]:
            lines.extend(
                [
                    str(row.get("hypothesis")),
                    f"Trades: {row.get('trades', 0)}",
                    f"Winrate: {row.get('winrate', 0)}%",
                    f"PF: {row.get('profit_factor', 0)}",
                    f"Saved Losses: {row.get('saved_losses', 0)}",
                    f"Lost Winners: {row.get('lost_winners', 0)}",
                    f"Net Benefit: {row.get('net_benefit', 0)}",
                    f"Verdict: {row.get('verdict', 'N/A')}",
                    "────────────",
                ]
            )
        lines.extend(
            [
                "Рекомендация:",
                str(report.get("recommendation", "Продолжить исследование.")),
                "",
                "Это исследовательские результаты, не сигнал к live-изменениям.",
            ]
        )
        return "\n".join(lines).rstrip("─").rstrip()

    error = run_readonly_module("strategy_lab/runner.py", timeout=120)
    if error:
        return f"Ошибка Strategy Lab: {' '.join(error.split())[:600]}"

    if command in {"", "compare"}:
        try:
            text = STRATEGY_LAB_SUMMARY_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        return text or "Strategy Lab пока недоступен. Попробуй позже."

    report = read_json(STRATEGY_LAB_REPORT_FILE)
    metrics = report.get("metrics", [])
    aliases = {
        "current": ["Current"],
        "momentum": ["Momentum+"],
        "news": ["News Filter"],
        "atr": ["ATR"],
        "edge": ["Edge"],
        "quality": ["Quality"],
    }
    names = aliases.get(command)
    if not names:
        return (
            "🧪 Strategy Lab\n\n"
            "Strategy Lab v1: /lab, /lab compare, /lab current, /lab quality\n\n"
            "Strategy Lab v2: /lab hypotheses, /lab cooldown, /lab trend, "
            "/lab momentum, /lab atr, /lab news, /lab edge, "
            "/lab duplicate, /lab volatility"
        )
    selected = [
        row for row in metrics
        if any(str(row.get("strategy", "")).startswith(name) for name in names)
    ]
    if not selected:
        return "Strategy Lab пока не нашёл данные по этому разделу."

    lines = [
        "🧪 Strategy Lab",
        "",
        "Shadow Research",
        "Live-стратегия не меняется.",
        "",
    ]
    for row in selected[:8]:
        lines.extend(
            [
                str(row.get("strategy")),
                f"Trades: {row.get('trades', 0)}",
                f"Winrate: {row.get('winrate', 0)}%",
                f"PF: {row.get('profit_factor', 0)}",
                f"Net R: {row.get('net_r', row.get('roi', 0))}",
                f"Skipped: {row.get('skipped_trades', 0)}",
                f"Status: {row.get('sample_status', 'N/A')}",
                "────────────",
            ]
        )
    lines.append("Это исследовательские результаты, не сигнал к live-изменениям.")
    return "\n".join(lines).rstrip("────────────").rstrip()


def format_shadow_replay_overview(report: Mapping[str, Any]) -> str:
    """Format the execution-aware Shadow Replay v2 overview."""
    sample = report.get("sample", {})
    metrics = report.get("metrics", {})
    ideal = metrics.get("ideal_all", {})
    effective = metrics.get("effective_portfolio", {})
    execution = report.get("execution_quality", {})
    return "\n".join([
        "🔁 Shadow Replay v2",
        "",
        f"Статус: {report.get('status', 'INSUFFICIENT_DATA')}",
        f"Сделки: {effective.get('trades', 0)}",
        f"Effective PF: {safe_float(effective.get('profit_factor')):.4f}",
        f"Ideal PF: {safe_float(ideal.get('profit_factor')):.4f}",
        f"Effective Net R: {safe_float(effective.get('net_r')):+.4f}",
        f"Ideal Net R: {safe_float(ideal.get('net_r')):+.4f}",
        f"Average Fee: {safe_float(execution.get('average_fee_r')):.4f} R",
        (
            "Average Slippage: "
            f"{safe_float(execution.get('average_slippage_impact_r')):+.4f} R"
        ),
        f"Average Delay: {safe_float(execution.get('average_delay_seconds')):.2f} сек",
        "",
        str(report.get("recommendation", "Продолжать Shadow Research.")),
        "",
        (
            "Полные R-метрики: "
            f"{sample.get('complete_metrics_total', 0)} / "
            f"{sample.get('closed_trades_total', 0)}"
        ),
        "/replay details",
        "",
        "Replay не влияет на LIVE.",
    ])


def format_shadow_replay_details(report: Mapping[str, Any]) -> str:
    """Format execution impacts, portfolio limits and latency scenarios."""
    metrics = report.get("metrics", {})
    impact = metrics.get("impact", {})
    execution = report.get("execution_quality", {})
    portfolio = report.get("portfolio", {})
    lines = [
        "🔁 Shadow Replay / Детали",
        "",
        f"Execution Quality: {execution.get('status', 'N/A')}",
        f"Комиссии: {safe_float(impact.get('fee_impact_r')):+.4f} R",
        f"Slippage: {safe_float(impact.get('slippage_impact_r')):+.4f} R",
        f"Funding: {safe_float(impact.get('funding_impact_r')):+.4f} R",
        f"Latency: {safe_float(impact.get('latency_impact_r')):+.4f} R",
        (
            "Общее влияние исполнения: "
            f"{safe_float(impact.get('total_execution_impact_r')):+.4f} R"
        ),
        "",
        "Replay Portfolio",
        f"Исполнено: {portfolio.get('trades_executed', 0)}",
        f"Пропущено: {portfolio.get('trades_skipped', 0)}",
        (
            "Максимальный риск: "
            f"{safe_float(portfolio.get('max_risk_used_pct')):.2f}%"
        ),
        f"Correlation warnings: {portfolio.get('correlation_warning_count', 0)}",
        "",
        "Latency scenarios",
    ]
    for scenario in report.get("latency_scenarios", []):
        if not isinstance(scenario, Mapping):
            continue
        result = scenario.get("effective_portfolio", {})
        lines.append(
            f"{scenario.get('latency_seconds', 0)} сек: "
            f"PF {safe_float(result.get('profit_factor')):.4f}, "
            f"Net R {safe_float(result.get('net_r')):+.4f}"
        )
    lines.extend([
        "",
        "Все параметры являются research-assumptions.",
        "LIVE-логика не изменялась.",
    ])
    return "\n".join(lines)


def format_replay(args: List[str] | None = None) -> str:
    """Format ready Shadow Replay v2, with legacy drill-down compatibility."""
    args = args or []
    command = args[0].strip().lower() if args else ""
    shadow_report = read_json(SHADOW_REPLAY_REPORT_FILE)
    if command in {"", "details", "summary"}:
        if not shadow_report:
            return (
                "🔁 Shadow Replay v2\n\n"
                "Готовый отчёт пока отсутствует.\n"
                "Запуск из терминала:\n"
                "venv/bin/python shadow_replay.py"
            )
        if command == "details":
            return format_shadow_replay_details(shadow_report)
        if command == "summary":
            try:
                text = SHADOW_REPLAY_SUMMARY_FILE.read_text(
                    encoding="utf-8"
                ).strip()
            except OSError:
                text = ""
            return text or format_shadow_replay_overview(shadow_report)
        return format_shadow_replay_overview(shadow_report)

    report = read_json(TRADE_REPLAY_REPORT_FILE)
    if not report:
        return (
            "🔁 Trade Replay Lab\n\n"
            "Готовый отчёт пока отсутствует.\n"
            "Запуск из терминала:\n"
            "venv/bin/python trade_replay_lab/replay_runner.py"
        )
    if command == "patterns":
        return format_replay_patterns(report)

    trades = [row for row in report.get("trades", []) if isinstance(row, Mapping)]
    if command == "last":
        selected = max(trades, key=lambda row: str(row.get("opened_at", "")), default={})
        return format_replay_trade(selected) if selected else format_replay_overview(report)
    if command:
        symbol = normalize_replay_symbol(command)
        matches = [row for row in trades if row.get("symbol") == symbol]
        if not matches:
            return (
                f"🔁 Trade Replay / {symbol}\n\n"
                "Закрытых сделок для этого символа в отчёте нет."
            )
        selected = max(matches, key=lambda row: str(row.get("opened_at", "")))
        return format_replay_trade(selected)
    return format_replay_overview(report)


def normalize_replay_symbol(value: str) -> str:
    """Normalize BTC, BTCUSDT or BTC/USDT for Replay lookup."""
    text = str(value or "").strip().upper().replace("-", "").replace("_", "")
    if "/" in text:
        return text
    if text.endswith("USDT"):
        return f"{text[:-4]}/USDT"
    return f"{text}/USDT"


def format_replay_overview(report: Mapping[str, Any]) -> str:
    """Format compact /replay overview."""
    sample = report.get("sample", {})
    summary = report.get("summary", {})
    top_reason = next(iter(summary.get("top_loss_reasons", []) or []), {})
    top_improvement = next(iter(summary.get("top_improvements", []) or []), {})
    return "\n".join([
        "🔁 Trade Replay Lab",
        "",
        f"Статус: {report.get('status', 'NO_DATA')}",
        f"Закрытых сделок: {sample.get('closed_trades', 0)}",
        f"WIN / LOSS: {sample.get('wins', 0)} / {sample.get('losses', 0)}",
        f"Winrate: {sample.get('winrate', 0)}%",
        f"OHLCV coverage: {sample.get('ohlcv_coverage', 0)}%",
        f"Средний Improvement Score: {summary.get('average_improvement_score', 0)}",
        "",
        f"Главная причина LOSS: {top_reason.get('reason', 'Недостаточно данных')}",
        f"Чаще помогало: {top_improvement.get('name', 'Недостаточно данных')}",
        "",
        str(report.get("recommendation", "Продолжать наблюдение.")),
        "",
        "/replay last | /replay BTC | /replay summary | /replay patterns",
    ])


def format_replay_trade(trade: Mapping[str, Any]) -> str:
    """Format one replayed closed trade."""
    if not trade:
        return "🔁 Trade Replay\n\nСделка не найдена."
    verdict = trade.get("verdict", {})
    improvements = trade.get("improvements", [])
    worsened = trade.get("what_would_worsen", [])
    memory = trade.get("memory", {})
    timeline = trade.get("scenarios", {}).get("timeline", {})
    result = str(trade.get("result", "N/A"))
    result_icon = "🟢" if result == "WIN" else "🔴" if result == "LOSS" else "⚪"
    lines = [
        "🔁 Trade Replay",
        "",
        f"{trade.get('symbol', 'N/A')} {trade.get('direction', '')}",
        f"Результат: {result_icon} {result}",
        f"Открыта: {trade.get('opened_at', 'N/A')}",
        f"Score {trade.get('score', 0)} | Confidence {trade.get('confidence', 0)}%",
        f"Quality {trade.get('quality') or 'N/A'} | Edge {trade.get('edge', 0)}",
        "",
        "Replay Verdict",
        f"Главная причина: {verdict.get('main_reason_label', 'Недостаточно данных')}",
    ]
    for reason in list(verdict.get("reason_labels", []))[1:5]:
        lines.append(f"- {reason}")
    lines.extend([
        "",
        f"Лучший вариант: {trade.get('best_variant', 'Нет подтверждённого варианта')}",
        f"Improvement Score: {trade.get('improvement_score', 0)}",
        f"Replay Confidence: {trade.get('replay_confidence', 0)}%",
    ])
    if improvements:
        lines.extend(["", "Что могло улучшить:"])
        for item in improvements[:4]:
            lines.append(
                f"- {item.get('label')}: ΔR {item.get('delta_r', 0)}"
            )
    if worsened:
        lines.extend(["", "Что могло ухудшить:"])
        for item in worsened[:3]:
            lines.append(
                f"- {item.get('label')}: ΔR {item.get('delta_r', 0)}"
            )
    available_timeline = [
        f"{horizon}: {data.get('return_pct', 0)}%"
        for horizon, data in timeline.items()
        if horizon in {"1h", "4h", "8h", "24h"} and data.get("available")
    ]
    if available_timeline:
        lines.extend(["", "Движение относительно входа:", *available_timeline])
    lines.extend([
        "",
        "Trade Memory",
        f"Похожих сделок: {memory.get('matches_count', 0)}",
        f"Winrate: {memory.get('winrate', 0)}% | PF: {memory.get('profit_factor', 0)}",
        "",
        "Replay носит исследовательский характер и не влияет на LIVE.",
    ])
    return "\n".join(lines)


def format_replay_patterns(report: Mapping[str, Any]) -> str:
    """Format recurring Replay Lab patterns."""
    patterns = report.get("patterns", [])
    lines = ["🔁 Replay Patterns", ""]
    if not patterns:
        return "\n".join([*lines, "Паттерны пока не найдены."])
    for index, row in enumerate(patterns[:10], start=1):
        lines.append(
            f"{index}. {row.get('reason_label') or row.get('reason')}"
        )
        lines.append(
            f"Сделок {row.get('count', 0)} | WIN {row.get('wins', 0)} | "
            f"LOSS {row.get('losses', 0)} | Confidence {row.get('confidence', 0)}%"
        )
    lines.extend(["", "Паттерны не применяются к LIVE автоматически."])
    return "\n".join(lines)


def format_consensus(args: List[str] | None = None) -> str:
    """Format ready Research Consensus report without running its engine."""
    report = read_json(RESEARCH_CONSENSUS_REPORT_FILE)
    if not report:
        return (
            "🧠 Research Consensus\n\n"
            "Готовый отчёт пока отсутствует.\n"
            "Запуск из терминала:\n"
            "venv/bin/python research_consensus/consensus_engine.py"
        )
    command = args[0].strip().lower() if args else ""
    if command == "summary":
        return format_consensus_summary(report)
    if command:
        return format_consensus_group(report, command)
    return format_consensus_overview(report)


def format_adaptive(args: List[str] | None = None) -> str:
    """Format ready Adaptive Research artifacts without running the pipeline."""
    report = read_json(ADAPTIVE_RESEARCH_REPORT_FILE)
    state = read_json(ADAPTIVE_RESEARCH_STATE_FILE)
    if not report:
        return (
            "🧠 Adaptive Research\n\n"
            "Готовый отчёт пока отсутствует.\n"
            "Запуск из терминала:\n"
            "venv/bin/python adaptive_research.py"
        )
    command = args[0].strip().lower() if args else ""
    if command == "status":
        return format_adaptive_status(report, state)
    if command in {"recommendation", "recommendations"}:
        return format_adaptive_recommendations(report)
    if command == "stages":
        return format_adaptive_stages(report)
    if command:
        return (
            "🧠 Adaptive Research\n\n"
            "Использование:\n"
            "/adaptive\n"
            "/adaptive status\n"
            "/adaptive recommendations\n"
            "/adaptive stages"
        )
    return format_adaptive_overview(report)


def format_promotion(args: List[str] | None = None) -> str:
    """Format the ready promotion report without running research modules."""
    report = read_json(EXPERIMENT_PROMOTION_REPORT_FILE)
    if not report:
        return (
            "🧪 Experiment Promotion\n\n"
            "Готовый отчёт пока отсутствует.\n"
            "Запуск из терминала:\n"
            "venv/bin/python experiment_promotion_engine.py"
        )
    command = args[0].strip().lower() if args else ""
    if command not in {"", "details"}:
        return (
            "🧪 Experiment Promotion\n\n"
            "Использование:\n"
            "/promotion\n"
            "/promotion details"
        )

    candidates = list(report.get("candidates", []) or [])
    promoted = list(report.get("promotion_candidates", []) or [])
    lines = [
        "🧪 Experiment Promotion",
        "",
        f"Статус: {report.get('status', 'INSUFFICIENT_DATA')}",
        f"Закрытых сделок: {report.get('closed_trades_total', 0)} / "
        f"{report.get('minimum_closed_trades', 50)}",
        f"Кандидатов на A/B-тест: {len(promoted)}",
        "",
    ]
    if not candidates:
        lines.append("Совместимых гипотез пока нет.")
    elif command == "details":
        lines.append("Подробный рейтинг:")
        for index, candidate in enumerate(candidates[:5], start=1):
            lines.extend([
                "",
                f"{index}. {candidate.get('candidate', 'Без названия')}",
                f"Confidence: {candidate.get('confidence', 0)}%",
                f"Risk: {candidate.get('risk', 'HIGH')}",
                f"Статус: {candidate.get('promotion_status', 'INSUFFICIENT_DATA')}",
                "Причины за:",
            ])
            lines.extend(
                f"+ {reason}"
                for reason in list(candidate.get("reasons_for", []) or [])[:3]
            )
            lines.append("Причины против:")
            lines.extend(
                f"- {reason}"
                for reason in list(candidate.get("reasons_against", []) or [])[:3]
            )
    else:
        lines.append("Лучшие наблюдения:")
        for index, candidate in enumerate(candidates[:5], start=1):
            lines.append(
                f"{index}. {candidate.get('candidate')} | "
                f"Confidence {candidate.get('confidence', 0)}% | "
                f"Risk {candidate.get('risk')}"
            )
    lines.extend([
        "",
        str(report.get("recommendation") or "Продолжить сбор статистики."),
        "",
        "Рекомендации не применяются к LIVE автоматически.",
    ])
    return "\n".join(lines)


def format_dashboard(args: Optional[List[str]] = None) -> str:
    """Format Research Dashboard v1 with legacy operational drill-downs."""
    section = args[0].strip().lower() if args else ""
    if section in {"trading", "live", "news", "lab", "memory"}:
        return format_live_dashboard(section)
    report = read_research_dashboard()
    if not report:
        return (
            "📊 Research Dashboard\n\n"
            "Status: NOT_AVAILABLE\n"
            "Запусти read-only отчёт:\n"
            "venv/bin/python research_dashboard.py"
        )
    return format_research_dashboard(report, section)


def format_datasources() -> str:
    """Show report provenance and freshness against the canonical trades file."""
    labels = {
        "research_dashboard": "Research Dashboard",
        "promotion_gate": "Promotion Gate",
        "decision_intelligence": "Decision Intelligence",
    }
    lines = ["🗂 Data Sources"]
    for key, row in datasource_status(base_dir=BASE_DIR).items():
        lines.extend([
            "",
            labels[key],
            "Source:",
            str(row["source"]),
            "Generated:",
            format_time(str(row["generated_at"])),
            "Trades Timestamp:",
            format_time(str(row["source_trades_modified"])),
            "Status:",
            str(row["status"]),
            "Sample:",
            str(row["sample"]),
        ])
    return "\n".join(lines)


def format_market() -> str:
    """Format the simplified UI v5 market screen."""
    return v5_format_market()


def format_opportunities() -> str:
    """Format the UI v5 opportunities screen."""
    return v5_format_opportunities()


def format_watchlist() -> str:
    """Format the simplified UI v5 watchlist."""
    return v5_format_watchlist()


def format_stats() -> str:
    """Format the simplified UI v5 statistics screen."""
    return v5_format_statistics()


def format_trades() -> str:
    """Format the simplified UI v5 trades screen."""
    return v5_format_trades()


def format_coach() -> str:
    """Format the recommendation-focused UI v5 AI Coach."""
    return v5_format_ai_coach()


def format_settings() -> str:
    """Format read-only UI settings."""
    return v5_format_settings()


def format_developer() -> str:
    """Format the developer section intro."""
    return v5_format_developer()


def format_dataquality() -> str:
    """Format the latest persisted Trade Registry quality report."""
    report = read_quality_report()
    if not report:
        return (
            "📋 Data Quality\n\n"
            "Status: NOT_AVAILABLE\n"
            "Запусти read-only реестр:\n"
            "venv/bin/python trade_registry.py"
        )
    if "coverage" in report:
        return format_research_data_quality(report)
    return format_data_quality_summary(report)


def format_coverage() -> str:
    report = read_quality_report()
    return format_research_data_quality(report, "coverage") if report else "📊 Data Coverage\nStatus: NOT_AVAILABLE"


def format_snapshot() -> str:
    path = BASE_DIR / "decision_snapshot.json"
    payload = read_json(path)
    snapshot = payload.get("latest", {}) if isinstance(payload, dict) else {}
    if not snapshot:
        snapshot = build_decision_snapshot(write=False)
    if not snapshot:
        return "📸 Decision Snapshot\nStatus: NOT_AVAILABLE"
    return "\n".join(["📸 Decision Snapshot", f"Symbol: {snapshot.get('symbol','UNKNOWN')}",
                      f"Direction: {snapshot.get('direction','UNKNOWN')}", f"Score: {snapshot.get('final_score','UNKNOWN')}",
                      f"Confidence: {snapshot.get('confidence','UNKNOWN')}", f"Quality: {snapshot.get('quality','UNKNOWN')}",
                      f"Regime: {snapshot.get('market_regime','UNKNOWN')}", f"Timeframe: {snapshot.get('timeframe','UNKNOWN')}"])


def format_portfolio_status(view: str = "") -> str:
    report = load_portfolio_report()
    if not report:
        report = PortfolioManager().portfolio_summary()
    return format_portfolio(report, view)


def format_execution_status(view: str = "") -> str:
    report = load_execution_report()
    requested_mode = "STRESS" if view == "stress" else None
    if not report or (requested_mode and report.get("mode") != requested_mode):
        report = ExecutionSimulator().run(requested_mode or "NORMAL")
    return format_execution(report, "summary" if view == "summary" else "")


def format_lossanalysis_status(view: str = "") -> str:
    report = load_loss_report()
    if not report:
        report = LossAttribution().build_report()
    return format_loss_analysis(report, view)


def format_signalquality_status(view: str = "") -> str:
    report = load_signal_quality()
    if not report:
        report = SignalQualityAnalyzer().build_report()
    return format_signal_quality(report, view)


def format_decisionv2_status(view: str = "") -> str:
    report = load_decision_v2()
    if not report:
        report = DecisionEngineV2().build_report()
    return format_decision_v2(report, view)


def format_walkforward() -> str:
    """Format the latest persisted Shadow Walk-Forward report."""
    report = read_json(WALK_FORWARD_REPORT_FILE)
    if not report:
        return (
            "📈 Walk Forward\n\n"
            "Status: NO_DATA\n"
            "Запусти read-only отчёт:\n"
            "venv/bin/python walk_forward_validator.py"
        )
    counts = report.get("verdict_counts", {})
    best = report.get("best", {})
    return "\n".join(
        [
            "📈 Walk Forward",
            f"Status: {report.get('status', 'INSUFFICIENT_DATA')}",
            f"Hypotheses: {report.get('hypotheses_count', 0)}",
            "READY_FOR_AB:",
            str(counts.get("READY_FOR_AB", 0)),
            "CONTINUE_RESEARCH:",
            str(counts.get("CONTINUE_RESEARCH", 0)),
            "REJECT:",
            str(counts.get("REJECT", 0)),
            "Best Stability:",
            str(best.get("hypothesis", "N/A")),
            "Stability:",
            str(best.get("stability_score", 0)),
            "Average PF:",
            str(best.get("average_pf", 0)),
            "Recommendation:",
            str(best.get("recommendation", "Continue Shadow Research")),
        ]
    )


def with_v5_footer(text: str) -> str:
    """Append the compact UI footer when a legacy formatter is reused."""
    if "Version: v1.0 / Telegram UI v5" in text:
        return text
    return text.rstrip() + v5_footer()


def truncate(text: str) -> str:
    """Keep messages inside Telegram limits."""
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text
    return text[:MAX_MESSAGE_LENGTH] + "\n\n... сообщение сокращено ..."


async def reply(
    update: Update,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Reply to a command with the main keyboard."""
    if update.message is None:
        return
    await update.message.reply_text(
        truncate(text),
        reply_markup=reply_markup or main_keyboard(),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register chat and show the health dashboard."""
    if update.effective_chat:
        save_chat_id(update.effective_chat.id)
    result = synchronize_reports(base_dir=BASE_DIR)
    if result["refreshed"]:
        await reply(update, "Dashboard is outdated.\nRefreshing research reports...\nDone.")
    if result["consistency"]["status"] != "PASSED":
        await reply(update, "Dashboard consistency check failed. Stale data will not be shown.")
        return
    await reply(update, format_dashboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show available commands."""
    await reply(update, help_text())


def help_text() -> str:
    """Return command help."""
    return "\n".join(
        [
            "🤖 AITradingAgent v1.0 / Telegram UI v5",
            "",
            "Ежедневная работа",
            "/dashboard",
            "/market",
            "/opportunities",
            "/watchlist",
            "/trades",
            "/stats",
            "/coach",
            "",
            "Сервис",
            "/start",
            "/help",
            "/settings",
            "/developer",
            "",
            "Developer-раздел",
            "Replay, Calibration, Experiments, Blocked, Research,",
            "Diagnostics, Dry Run, Reports.",
            "",
            "Прямые команды для глубокой аналитики всё ещё доступны:",
            "/diagnostics BTC",
            "/dashboard hypotheses|conflicts|status|history",
            "/dashboard trading|live|news|lab|memory",
            "/blocked Momentum|Structure|Risk|Trend|ALL",
            "/research /experiments /calibration /quality",
            "/news sources|health|BTC|risks|stale",
            "/heatmap /intelligence /memory BTC /context",
            "/live /live SOL /live trades /live setups /system",
            "/lab hypotheses|cooldown|trend|momentum|atr|news|edge",
            "/lab duplicate|volatility|compare|current|quality",
            "/replay last|BTC|summary|patterns",
            "/consensus momentum|edge|news|summary",
            "/adaptive status|recommendations|stages",
            "/promotion details",
            "/walkforward",
            "/dataquality | /trades health",
            "/datasources",
        ]
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, format_status())


async def dashboard_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    section = context.args[0].strip().lower() if context.args else ""
    if section not in {"trading", "live", "news", "lab", "memory"}:
        result = synchronize_reports(base_dir=BASE_DIR)
        if result["refreshed"]:
            await reply(update, "Dashboard is outdated.\nRefreshing research reports...\nDone.")
        if result["consistency"]["status"] != "PASSED":
            await reply(update, "Dashboard consistency check failed. Stale data will not be shown.")
            return
    await reply(update, format_dashboard(context.args))


async def coach_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_coach())


async def v2_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, format_v2_readiness())


async def symbols_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_symbols_report())


async def equity_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_equity_report())


async def timeline_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_timeline_report())


async def market_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, format_market(), v5_market_keyboard(v5_market_symbols()))


async def opportunities_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_opportunities())


async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, format_watchlist())


async def diagnostics_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    symbol = context.args[0] if context.args else ""
    if not symbol:
        await reply(update, "🧠 Диагностика\n\nИспользование: /diagnostics BTC")
        return
    await reply(update, format_diagnostics(symbol))


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, format_stats())


async def trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args and context.args[0].strip().lower() == "health":
        await reply(update, format_dataquality())
        return
    await reply(update, format_trades())


async def dataquality_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_dataquality())


async def coverage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, format_coverage())


async def backfill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    report = run_backfill_pipeline()
    await reply(update, "\n".join(["🧰 Research Backfill", f"Recovered: {report.get('recovered_total',0)} fields",
                                   f"Unable: {report.get('unable_total',0)} fields", "LIVE trades.csv unchanged."]))


async def snapshot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, format_snapshot())


async def portfolio_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    view = context.args[0].strip().lower() if context.args else ""
    await reply(update, format_portfolio_status(view))


async def execution_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    view = context.args[0].strip().lower() if context.args else ""
    await reply(update, format_execution_status(view))


async def lossanalysis_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    view = context.args[0].strip().lower() if context.args else ""
    await reply(update, format_lossanalysis_status(view))


async def decisionv2_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    view = context.args[0].strip().lower() if context.args else ""
    await reply(update, format_decisionv2_status(view))


async def settings_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_settings())


async def developer_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_developer(), v5_developer_keyboard())


async def posttrade_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_posttrade())


async def calibration_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_calibration())


async def research_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    section = context.args[0].strip().lower() if context.args else "overview"
    await reply(update, format_research(section))


async def experiments_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    is_full = bool(context.args and context.args[0].lower() == "full")
    await reply(update, format_experiments(full=is_full))


async def candidate_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_candidate())


async def ready_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rebuild and show the read-only VPS promotion gate."""
    synchronize_reports(base_dir=BASE_DIR)
    await reply(update, format_promotion_gate(read_json(BASE_DIR / "reports/promotion_gate.json")))


async def learning_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    synchronize_reports(base_dir=BASE_DIR)
    await reply(update, format_decision_learning(read_json(BASE_DIR / "decision_learning.json"), "learning"))


async def modules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    synchronize_reports(base_dir=BASE_DIR)
    await reply(update, format_decision_learning(read_json(BASE_DIR / "decision_learning.json"), "modules"))


async def accuracy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    synchronize_reports(base_dir=BASE_DIR)
    await reply(update, format_decision_learning(read_json(BASE_DIR / "decision_learning.json"), "accuracy"))


async def rootcause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    synchronize_reports(base_dir=BASE_DIR)
    await reply(update, format_decision_learning(read_json(BASE_DIR / "decision_learning.json"), "rootcause"))


async def datasources_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, format_datasources())


async def learn_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_learn())


async def quality_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    view = context.args[0].strip().lower() if context.args else ""
    await reply(update, format_signalquality_status(view))


async def filters_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_filters())


async def blocked_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    blocker = context.args[0] if context.args else "Momentum"
    normalized = normalize_blocker(blocker)
    if normalized is None:
        await reply(
            update,
            "🚧 Blocked\n\n"
            "Использование: /blocked Momentum|Structure|Risk|Trend|ALL",
        )
        return
    await reply(update, format_blocked(normalized))


async def regime_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_regime())


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, format_history())


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, format_daily_report())


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    section = context.args[0] if context.args else "overview"
    await reply(update, format_news(section))


async def heatmap_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, format_heatmap())


async def live_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    try:
        text = format_live_monitor(context.args)
    except Exception as exc:  # noqa: BLE001 - Telegram command must answer
        print(f"Ошибка /live formatter: {type(exc).__name__}: {exc}")
        text = (
            "📡 Live / Сделки\n\n"
            "Данные Live Monitor временно недоступны. Попробуй позже."
            if context.args and context.args[0].strip().lower() == "trades"
            else "📡 Live Monitor\n\nДанные временно недоступны. Попробуй позже."
        )
    if not str(text or "").strip():
        text = (
            "📡 Live / Сделки\n\nОткрытых сделок сейчас нет."
            if context.args and context.args[0].strip().lower() == "trades"
            else "📡 Live Monitor\n\nДанные пока недоступны."
        )
    await reply(update, text)


async def system_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_live_system())


async def intelligence_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_intelligence())


async def memory_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    symbol = context.args[0] if context.args else ""
    await reply(update, format_memory(symbol))


async def context_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_context())


async def lab_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_lab(context.args))


async def replay_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_replay(context.args))


async def consensus_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_consensus(context.args))


async def adaptive_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_adaptive(context.args))


async def walkforward_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Return the latest report without running or applying research."""
    await reply(update, format_walkforward())


async def promotion_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_promotion(context.args))


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard callbacks."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    reply_markup = main_keyboard()
    if query.data and query.data.startswith("symbol:"):
        text = v5_format_symbol_detail(query.data.split(":", 1)[1])
        reply_markup = v5_market_keyboard(v5_market_symbols())
    elif query.data and query.data.startswith("dev:"):
        developer_actions = {
            "dev:replay": lambda: format_replay([]),
            "dev:experiments": lambda: with_v5_footer(format_experiments(full=False)),
            "dev:research": lambda: with_v5_footer(format_research()),
            "dev:diagnostics": lambda: (
                "🧠 Diagnostics\n\n"
                "Для подробностей используй /diagnostics BTC, /diagnostics ETH и т.д."
            ),
            "dev:dryrun": v5_format_dry_run,
            "dev:reports": v5_format_reports_status,
        }
        text = developer_actions.get(query.data, format_developer)()
        if query.data in {"dev:replay", "dev:diagnostics"}:
            text = with_v5_footer(text)
        reply_markup = v5_developer_keyboard()
    elif query.data and query.data.startswith("blocked:"):
        blocker = normalize_blocker(query.data.split(":", 1)[1])
        text = (
            "🚧 Blocked\n\n"
            "Некорректный blocker для inline-кнопки."
            if blocker is None
            else format_blocked(blocker)
        )
    elif query.data == "regime:view":
        text = format_regime()
    else:
        if query.data == "dashboard":
            result = synchronize_reports(base_dir=BASE_DIR)
            if result["consistency"]["status"] != "PASSED":
                await query.edit_message_text(
                    text="Dashboard consistency check failed. Stale data will not be shown.",
                    reply_markup=reply_markup,
                )
                return
        actions = {
            "dashboard": format_dashboard,
            "coach": format_coach,
            "opportunities": format_opportunities,
            "market": format_market,
            "watchlist": format_watchlist,
            "stats": format_stats,
            "trades": format_trades,
            "settings": format_settings,
            "developer": format_developer,
        }
        text = actions.get(query.data, help_text)()
        if query.data == "market":
            reply_markup = v5_market_keyboard(v5_market_symbols())
        elif query.data == "developer":
            reply_markup = v5_developer_keyboard()

    try:
        await query.edit_message_text(
            text=truncate(text),
            reply_markup=reply_markup,
        )
    except BadRequest as exc:
        if "Message is not modified" in str(exc):
            return
        await query.message.reply_text(
            truncate(text),
            reply_markup=reply_markup,
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Print compact runtime errors for Telegram polling."""
    print(f"Ошибка Telegram handler: {context.error}")


async def register_bot_commands(app) -> None:
    """Register persistent Telegram menu commands on startup."""
    await app.bot.set_my_commands(BOT_COMMANDS)


def build_app():
    """Build the Telegram application."""
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не найден. Проверьте .env.")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(register_bot_commands).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("dashboard", dashboard_command))
    app.add_handler(CommandHandler("coach", coach_command))
    app.add_handler(CommandHandler("opportunities", opportunities_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("developer", developer_command))
    app.add_handler(CommandHandler("v2", v2_command))
    app.add_handler(CommandHandler("symbols", symbols_command))
    app.add_handler(CommandHandler("equity", equity_command))
    app.add_handler(CommandHandler("timeline", timeline_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("market", market_command))
    app.add_handler(CommandHandler("watchlist", watchlist_command))
    app.add_handler(CommandHandler("diagnostics", diagnostics_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("trades", trades_command))
    app.add_handler(CommandHandler("dataquality", dataquality_command))
    app.add_handler(CommandHandler("coverage", coverage_command))
    app.add_handler(CommandHandler("backfill", backfill_command))
    app.add_handler(CommandHandler("snapshot", snapshot_command))
    app.add_handler(CommandHandler("portfolio", portfolio_command))
    app.add_handler(CommandHandler("execution", execution_command))
    app.add_handler(CommandHandler("lossanalysis", lossanalysis_command))
    app.add_handler(CommandHandler("decisionv2", decisionv2_command))
    app.add_handler(CommandHandler("posttrade", posttrade_command))
    app.add_handler(CommandHandler("calibration", calibration_command))
    app.add_handler(CommandHandler("research", research_command))
    app.add_handler(CommandHandler("experiments", experiments_command))
    app.add_handler(CommandHandler("candidate", candidate_command))
    app.add_handler(CommandHandler("learn", learn_command))
    app.add_handler(CommandHandler("quality", quality_command))
    app.add_handler(CommandHandler("filters", filters_command))
    app.add_handler(CommandHandler("blocked", blocked_command))
    app.add_handler(CommandHandler("regime", regime_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("news", news_command))
    app.add_handler(CommandHandler("heatmap", heatmap_command))
    app.add_handler(CommandHandler("live", live_command))
    app.add_handler(CommandHandler("system", system_command))
    app.add_handler(CommandHandler("intelligence", intelligence_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("context", context_command))
    app.add_handler(CommandHandler("lab", lab_command))
    app.add_handler(CommandHandler("replay", replay_command))
    app.add_handler(CommandHandler("consensus", consensus_command))
    app.add_handler(CommandHandler("adaptive", adaptive_command))
    app.add_handler(CommandHandler("walkforward", walkforward_command))
    app.add_handler(CommandHandler("promotion", promotion_command))
    app.add_handler(CommandHandler("ready", ready_command))
    app.add_handler(CommandHandler("learning", learning_command))
    app.add_handler(CommandHandler("modules", modules_command))
    app.add_handler(CommandHandler("accuracy", accuracy_command))
    app.add_handler(CommandHandler("rootcause", rootcause_command))
    app.add_handler(CommandHandler("datasources", datasources_command))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    """Run polling."""
    app = build_app()
    print("Telegram-интерфейс запущен")
    try:
        app.run_polling()
    except Conflict:
        print(
            "Конфликт polling: другой экземпляр бота уже использует "
            "getUpdates."
        )
    except NetworkError as exc:
        print(
            "Ошибка сети Telegram: бот не смог подключиться к API. "
            f"Детали: {exc}"
        )


if __name__ == "__main__":
    main()
