"""Telegram assistant for AITradingAgent v3."""

from __future__ import annotations

import csv
import json
import os
import subprocess
from datetime import datetime, timezone
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

from config import RUN_INTERVAL
from calibration_report import build_calibration_report, save_report
from decision_diagnostics import DecisionDiagnostics
from notification_manager import save_chat_id
from telegram_formatters import (
    format_ai_coach as v5_format_ai_coach,
    format_dashboard as v5_format_dashboard,
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
    market_symbols as v5_market_symbols,
)
from telegram_handlers import (
    BOT_COMMANDS_V5,
    developer_keyboard as v5_developer_keyboard,
    main_keyboard as v5_main_keyboard,
    market_keyboard as v5_market_keyboard,
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
MARKET_HEATMAP_FILE = BASE_DIR / "market_heatmap_report.json"
MARKET_INTELLIGENCE_FILE = BASE_DIR / "market_intelligence_report.json"
MARKET_INTELLIGENCE_SUMMARY_FILE = BASE_DIR / "market_intelligence_summary.txt"
TRADE_MARKET_CONTEXT_FILE = BASE_DIR / "trade_market_context.csv"
TRADE_MEMORY_SUMMARY_FILE = BASE_DIR / "trade_memory_summary.txt"
STATS_FILE = BASE_DIR / "agent_v3_stats.json"
TRADES_FILE = BASE_DIR / "trades.csv"
WEIGHTS_FILE = BASE_DIR / "strategy_weights.json"
VENV_PYTHON = BASE_DIR / "venv" / "bin" / "python"

MAX_MESSAGE_LENGTH = 3900
FRESHNESS_GRACE_SECONDS = 60 * 60
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


def format_dashboard() -> str:
    """Build a one-screen health dashboard from existing reports."""
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

    max_drawdown = "N/A"
    baseline_metrics = {}
    for row in experiments.get("results", []):
        if row.get("scenario") == "baseline":
            baseline_metrics = row.get("trade_metrics", {})
            break
    if baseline_metrics:
        max_drawdown = baseline_metrics.get("max_drawdown", "N/A")

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
            f"📉 Max Drawdown: {max_drawdown}",
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
    """Calculate trade stats from trades.csv."""
    rows = read_csv_rows(TRADES_FILE)
    closed = [row for row in rows if row.get("status") in {"WIN", "LOSS"}]
    open_trades = [row for row in rows if row.get("status") == "OPEN"]
    wins = [row for row in closed if row.get("status") == "WIN"]
    losses = [row for row in closed if row.get("status") == "LOSS"]

    gross_profit = sum(max(safe_float(row.get("pnl")), 0) for row in closed)
    gross_loss = sum(abs(min(safe_float(row.get("pnl")), 0)) for row in closed)
    win_rate = len(wins) / len(closed) * 100 if closed else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss else 0.0

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
        "closed": len(closed),
        "open": len(open_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_rr": avg_rr,
    }


def closed_trade_rows() -> List[Dict[str, str]]:
    """Return closed trades from trades.csv in chronological order."""
    rows = [
        row for row in read_csv_rows(TRADES_FILE)
        if row.get("status") in {"WIN", "LOSS"}
        or row.get("result") in {"WIN", "LOSS"}
    ]
    return sorted(rows, key=lambda row: row.get("closed_at") or row.get("opened_at", ""))


def trade_metrics(rows: Iterable[Mapping[str, str]]) -> Dict[str, Any]:
    """Calculate compact trade metrics for an iterable of trade rows."""
    items = list(rows)
    wins = [
        row for row in items
        if (row.get("status") or row.get("result")) == "WIN"
    ]
    losses = [
        row for row in items
        if (row.get("status") or row.get("result")) == "LOSS"
    ]
    pnls = [safe_float(row.get("pnl")) for row in items]
    gross_profit = sum(max(pnl, 0.0) for pnl in pnls)
    gross_loss = sum(abs(min(pnl, 0.0)) for pnl in pnls)
    return {
        "trades": len(items),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round((len(wins) / len(items) * 100) if items else 0.0, 2),
        "profit_factor": round((gross_profit / gross_loss) if gross_loss else 0.0, 2),
        "average_pnl": round((sum(pnls) / len(pnls)) if pnls else 0.0, 2),
        "pnl": round(sum(pnls), 2),
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
        key=lambda item: (item[1]["pnl"], item[1]["profit_factor"], item[1]["winrate"]),
        reverse=True,
    )
    best = ranked[0][0]
    worst = ranked[-1][0]
    lines = ["🏆 Symbols", "", f"Лучший символ: {best}", f"Худший символ: {worst}", ""]
    for symbol, metrics in ranked:
        lines.append(
            f"{symbol}: WR {metrics['winrate']}% | PF {metrics['profit_factor']} | "
            f"trades {metrics['trades']} | avg PnL {metrics['average_pnl']}"
        )
    return "\n".join(lines)


def format_equity_report() -> str:
    """Format equity and PnL report from closed trades."""
    trades = closed_trade_rows()
    if not trades:
        return "💰 Equity\n\nЗакрытых сделок пока нет."
    pnls = [safe_float(row.get("pnl")) for row in trades]
    total_pnl = round(sum(pnls), 2)
    stats = read_json(STATS_FILE)
    start_balance = safe_float(stats.get("start_balance"))
    current_balance = (
        round(start_balance + total_pnl, 2)
        if start_balance else "N/A"
    )
    streaks = best_worst_streaks(trades)
    by_day: Dict[str, float] = {}
    for row in trades:
        closed_at = parse_time(row.get("closed_at", ""))
        day = closed_at.date().isoformat() if closed_at else "N/A"
        by_day[day] = by_day.get(day, 0.0) + safe_float(row.get("pnl"))
    best_day = max(by_day, key=by_day.get) if by_day else "N/A"
    worst_day = min(by_day, key=by_day.get) if by_day else "N/A"
    return "\n".join(
        [
            "💰 Equity",
            "",
            f"Общий PnL: {total_pnl}",
            f"Текущий баланс: {current_balance}",
            f"Max Drawdown: {max_drawdown_from_pnls(pnls)}",
            f"Лучшая серия побед: {streaks['best_win']}",
            f"Худшая серия поражений: {streaks['worst_loss']}",
            f"Лучший день: {best_day} ({round(by_day.get(best_day, 0), 2)})",
            f"Худший день: {worst_day} ({round(by_day.get(worst_day, 0), 2)})",
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
    current_dd = max_drawdown_from_pnls(safe_float(row.get("pnl")) for row in trades)
    early_dd = max_drawdown_from_pnls(safe_float(row.get("pnl")) for row in first_window)
    first_date = parse_time(trades[0].get("closed_at", "")) or parse_time(trades[0].get("opened_at", ""))
    last_date = parse_time(trades[-1].get("closed_at", "")) or parse_time(trades[-1].get("opened_at", ""))
    lines = [
        "🕘 Timeline",
        "",
        f"Период: {first_date.date().isoformat() if first_date else 'N/A'} -> {last_date.date().isoformat() if last_date else 'N/A'}",
        f"Сделки: {early['trades']} -> {current['trades']}",
        f"Winrate: {early['winrate']}% -> {current['winrate']}%",
        f"Profit Factor: {early['profit_factor']} -> {current['profit_factor']}",
        f"Max Drawdown: {early_dd} -> {current_dd}",
    ]
    if baseline:
        lines.extend(
            [
                "",
                "Experiments baseline:",
                f"WR: {baseline.get('winrate', 0)}%",
                f"PF: {baseline.get('profit_factor', 0)}",
                f"Max DD: {baseline.get('max_drawdown', 0)}",
            ]
        )
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


def format_research() -> str:
    """Format strategy research for Telegram."""
    error = ensure_research_report()
    if error:
        return f"🔬 Research\n\n{error}"

    report = read_json(RESEARCH_REPORT_FILE)
    if not report:
        return (
            "🔬 Research\n\n"
            "Файл strategy_research_report.json пуст или повреждён."
        )
    summary_generated_at = "N/A"
    if RESEARCH_SUMMARY_FILE.exists():
        summary_text = RESEARCH_SUMMARY_FILE.read_text(encoding="utf-8")
        for line in summary_text.splitlines():
            if line.startswith("Generated at:"):
                summary_generated_at = line.split(":", 1)[1].strip()
                break

    decision_overview = report.get("decision_overview", {})
    diagnostics_overview = report.get("diagnostics_overview", {})
    trade_overview = report.get("trade_overview", {})

    blockers = diagnostics_overview.get("primary_blockers", {})
    primary_blocker = "N/A"
    if blockers:
        primary_blocker = max(
            blockers.items(),
            key=lambda item: safe_float(item[1]),
        )[0]

    near_setup = diagnostics_overview.get("near_setup_symbols", {})
    near_lines = []
    for symbol, payload in list(near_setup.items())[:3]:
        near_lines.append(
            f"- {symbol}: near={payload.get('near_setup_count', 0)}, "
            f"avg_potential={payload.get('average_potential_score', 0)}"
        )
    if not near_lines:
        near_lines.append("- Пока нет данных")

    recommendations = report.get("recommendations", [])
    recommendation_lines = [
        f"- {localize_research_item(item)}" for item in recommendations[:4]
    ] or ["- Пока нет рекомендаций"]

    lines = [
        "🔬 Research",
        "",
        f"Отчёт обновлён: {summary_generated_at}",
        f"Всего решений: {decision_overview.get('total_rows', 0)}",
        f"NO TRADE: {decision_overview.get('signal_counts', {}).get('NO TRADE', 0)}",
        f"Главный blocker: {primary_blocker}",
        (
            "Средний lost_score: "
            f"{diagnostics_overview.get('average_lost_score', 0)}"
        ),
        f"Winrate: {trade_overview.get('winrate', 0)}%",
        f"Average PnL: {trade_overview.get('average_pnl', 0)}",
        "",
        "Near-setup лидеры:",
        *near_lines,
        "",
        "Главные рекомендации:",
        *recommendation_lines,
    ]
    return "\n".join(lines)


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


def format_news() -> str:
    """Format market news observer output for Telegram."""
    if not MARKET_NEWS_FILE.exists() or MARKET_NEWS_FILE.stat().st_size == 0:
        error = run_readonly_module("market_news_observer.py", "--offline")
        if error:
            return f"📰 Новости рынка\n\n{error}"

    report = read_json(MARKET_NEWS_FILE)
    if not report:
        return (
            "📰 Новости рынка\n\n"
            "Файл market_news_feed.json пока отсутствует или повреждён.\n"
            "Для обновления запусти: venv/bin/python market_news_observer.py"
        )

    summary = report.get("summary", {})
    news_items = report.get("news", [])
    lines = [
        "📰 Новости рынка",
        "",
        f"Статус: {report.get('status', 'N/A')}",
        f"Настроение: {summary.get('market_sentiment', 'Neutral')}",
        f"Новостей за 24ч: {summary.get('recent_24h', 0)}",
        "",
        "Последние новости:",
    ]
    for item in news_items[:5]:
        lines.append(
            f"{item.get('coin', 'MARKET')} | {item.get('sentiment', 'Neutral')} "
            f"{item.get('strength', 1)}/5"
        )
        lines.append(str(item.get("title", "Без заголовка"))[:160])
    if not news_items:
        lines.append("Пока нет загруженных новостей.")
        lines.append("Обновление: venv/bin/python market_news_observer.py")
    if report.get("warnings"):
        lines.append("")
        lines.append("Предупреждения:")
        lines.extend(f"- {warning}" for warning in report["warnings"][:3])
    lines.append("")
    lines.append("Новости не влияют на сделки.")
    return "\n".join(lines)


def format_heatmap() -> str:
    """Format market heatmap output for Telegram."""
    error = run_readonly_module("market_heatmap.py")
    if error:
        return f"🗺 Heatmap\n\n{error}"
    report = read_json(MARKET_HEATMAP_FILE)
    if not report:
        return "🗺 Heatmap\n\nФайл market_heatmap_report.json пуст или повреждён."

    lines = ["🗺 Heatmap", ""]
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
    lines.append("")
    lines.append("Heatmap только показывает контекст рынка.")
    return "\n".join(lines)


def format_intelligence() -> str:
    """Format combined market intelligence summary."""
    error = run_readonly_module("market_intelligence_hub.py")
    if error:
        return f"🧠 Market Intelligence\n\n{error}"
    if MARKET_INTELLIGENCE_SUMMARY_FILE.exists():
        text = MARKET_INTELLIGENCE_SUMMARY_FILE.read_text(encoding="utf-8").strip()
        if text:
            return "🧠 Market Intelligence\n\n" + text
    report = read_json(MARKET_INTELLIGENCE_FILE)
    if not report:
        return (
            "🧠 Market Intelligence\n\n"
            "Файл market_intelligence_report.json пуст или повреждён."
        )
    return "\n".join(
        [
            "🧠 Market Intelligence",
            "",
            f"Рынок: {report.get('market', {}).get('regime', 'Недостаточно данных')}",
            f"Новости: {report.get('market', {}).get('news_sentiment', 'Neutral')}",
            f"Рекомендация: {report.get('recommendation', 'N/A')}",
            f"Следующее исследование: {report.get('next_research', 'N/A')}",
        ]
    )


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
    """Format latest trade market context."""
    error = run_readonly_module("trade_market_context.py")
    if error:
        return f"🔎 Контекст сделок\n\n{error}"
    rows = read_csv_rows(TRADE_MARKET_CONTEXT_FILE)
    if not rows:
        return (
            "🔎 Контекст сделок\n\n"
            "trade_market_context.csv пока пуст. Сделок для контекста нет."
        )

    lines = ["🔎 Контекст сделок", ""]
    for row in rows[-5:]:
        lines.append(
            f"{row.get('symbol', 'N/A')} {row.get('direction', '')} "
            f"{row.get('result') or row.get('status') or 'N/A'}"
        )
        lines.append(
            f"Score {row.get('score', 'N/A')} | "
            f"Confidence {row.get('confidence', 'N/A')} | "
            f"Edge {row.get('directional_edge', 'N/A')}"
        )
        lines.append(
            f"Momentum {row.get('momentum', 'N/A')} | "
            f"Trend {row.get('trend', 'N/A')} | "
            f"News {row.get('news_sentiment', 'Neutral')}"
        )
        if row.get("context_notes"):
            lines.append(f"Контекст: {row.get('context_notes')}")
        lines.append("────────────")
    lines.append("Контекст не влияет на открытие/закрытие сделок.")
    return "\n".join(lines).rstrip("────────────").rstrip()


def format_dashboard() -> str:
    """Format the simplified UI v5 dashboard."""
    return v5_format_dashboard()


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
            "/blocked Momentum|Structure|Risk|Trend|ALL",
            "/research /experiments /calibration /quality",
            "/news /heatmap /intelligence /memory BTC /context",
        ]
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, format_status())


async def dashboard_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_dashboard())


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
    await reply(update, format_trades())


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
    await reply(update, format_research())


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


async def learn_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_learn())


async def quality_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await reply(update, format_quality())


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
    await reply(update, format_news())


async def heatmap_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, format_heatmap())


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
            "dev:replay": lambda: (
                "Replay report доступен через strategy_replay_report.json."
                if read_json(BASE_DIR / "strategy_replay_report.json")
                else "Replay report пока отсутствует."
            ),
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
    app.add_handler(CommandHandler("intelligence", intelligence_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("context", context_command))
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
