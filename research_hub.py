"""Research Hub v1: unified read-only research conclusions for AITradingAgent."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


BASE_DIR = Path(__file__).resolve().parent

SOURCES = {
    "strategy_replay": BASE_DIR / "strategy_replay_report.json",
    "decision_pipeline": BASE_DIR / "decision_pipeline_profile_report.json",
    "opportunity_expansion": BASE_DIR / "trade_opportunity_expansion_report.json",
    "market_regime": BASE_DIR / "market_regime_advisor_report.json",
    "dataset_quality": BASE_DIR / "dataset_quality_report.json",
    "trade_comparator": BASE_DIR / "trade_comparator_report.json",
    "sl_quality": BASE_DIR / "sl_entry_quality_experiment_report.json",
    "confidence_calibration": BASE_DIR / "confidence_calibration_experiment_report.json",
    "agent_stats": BASE_DIR / "agent_v3_stats.json",
    "signals": BASE_DIR / "signals_v3.csv",
    "trades": BASE_DIR / "trades.csv",
    "ada_dry_run": BASE_DIR / "ada_opportunity_dry_run.csv",
    "doge_link_dry_run": BASE_DIR / "doge_link_opportunity_dry_run.csv",
    "protective_dry_run": BASE_DIR / "protective_filter_dry_run.csv",
    "sl_quality_dry_run": BASE_DIR / "sl_quality_protective_dry_run.csv",
    "confidence_sl_dry_run": BASE_DIR / "confidence_sl_quality_d_dry_run.csv",
}

REPORT_FILE = BASE_DIR / "research_hub_report.json"
SUMMARY_FILE = BASE_DIR / "research_hub_summary.txt"
HYPOTHESES_FILE = BASE_DIR / "research_hub_hypotheses.csv"


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


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
    """Convert values from reports to float."""
    try:
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


def latest_row(rows: list[dict[str, str]]) -> dict[str, str]:
    """Return latest row by timestamp."""
    if not rows:
        return {}
    return max(rows, key=lambda row: row.get("timestamp", ""))


def signal_weighted_score(row: Mapping[str, str]) -> float:
    """Return strongest stored score from signal/debug-like rows."""
    return max(
        safe_float(row.get("weighted_score")),
        safe_float(row.get("long_total")),
        safe_float(row.get("short_total")),
        safe_float(row.get("score")),
    )


def signal_edge(row: Mapping[str, str]) -> float:
    """Return directional edge when available."""
    return safe_float(row.get("diff"))


def is_near_setup(row: Mapping[str, str]) -> bool:
    """Detect near setup from stored signal values."""
    signal = row.get("signal", "")
    if signal in {"WATCH", "SETUP", "HIGH PRIORITY"}:
        return True
    return (
        signal == "NO TRADE"
        and safe_float(row.get("score")) == 0
        and safe_float(row.get("confidence")) >= 60
        and signal_weighted_score(row) >= 18
        and signal_edge(row) >= 7
    )


def trade_metrics(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Calculate compact trade statistics."""
    closed = [
        row for row in rows
        if (row.get("status") or row.get("result")) in {"WIN", "LOSS"}
    ]
    open_rows = [row for row in rows if row.get("status") == "OPEN"]
    wins = [
        row for row in closed
        if (row.get("status") or row.get("result")) == "WIN"
    ]
    losses = [
        row for row in closed
        if (row.get("status") or row.get("result")) == "LOSS"
    ]
    pnls = [safe_float(row.get("pnl")) for row in closed]
    gross_profit = sum(max(pnl, 0.0) for pnl in pnls)
    gross_loss = sum(abs(min(pnl, 0.0)) for pnl in pnls)
    return {
        "open": len(open_rows),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round((len(wins) / len(closed) * 100) if closed else 0.0, 2),
        "profit_factor": round((gross_profit / gross_loss) if gross_loss else 0.0, 4),
        "net_pnl": round(sum(pnls), 2),
    }


def dry_run_count(path: Path) -> int:
    """Count dry-run candidates."""
    return len(read_csv_rows(path))


def file_state(path: Path) -> dict[str, Any]:
    """Return source availability metadata."""
    return {
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else 0,
        "rows": len(read_csv_rows(path)) if path.suffix == ".csv" else None,
    }


class ResearchHub:
    """Read-only aggregator of existing research conclusions."""

    def __init__(self) -> None:
        self.json_sources = {
            name: read_json(path)
            for name, path in SOURCES.items()
            if path.suffix == ".json"
        }
        self.csv_sources = {
            name: read_csv_rows(path)
            for name, path in SOURCES.items()
            if path.suffix == ".csv"
        }
        self.warnings = [
            f"Файл отсутствует или пуст: {path.name}"
            for path in SOURCES.values()
            if not path.exists() or path.stat().st_size == 0
        ]

    def build_status(self) -> dict[str, Any]:
        """Build overall system status."""
        stats = self.json_sources.get("agent_stats", {})
        signals = self.csv_sources.get("signals", [])
        debug_rows = read_csv_rows(BASE_DIR / "decision_debug.csv")
        trades = self.csv_sources.get("trades", [])
        latest_signal = latest_row(signals)
        latest_time = parse_time(latest_signal.get("timestamp", ""))
        online = bool(
            latest_time
            and datetime.now(timezone.utc) - latest_time <= timedelta(minutes=45)
        )
        metrics = trade_metrics(trades)
        market = self.json_sources.get("market_regime", {})
        opportunity = self.json_sources.get("opportunity_expansion", {})

        decision_rows = debug_rows or signals
        latest_by_symbol: dict[str, dict[str, str]] = {}
        for row in decision_rows:
            symbol = row.get("symbol", "")
            if symbol and row.get("timestamp", "") >= latest_by_symbol.get(symbol, {}).get("timestamp", ""):
                latest_by_symbol[symbol] = row
        near_rows = [row for row in latest_by_symbol.values() if is_near_setup(row)]
        best = sorted(
            near_rows or list(latest_by_symbol.values()),
            key=lambda row: (
                is_near_setup(row),
                safe_float(row.get("confidence")),
                signal_weighted_score(row),
                signal_edge(row),
            ),
            reverse=True,
        )
        best_symbol = (
            best[0].get("symbol", "N/A").replace("/USDT", "")
            if best else "N/A"
        )

        dry_run_candidates = sum(
            dry_run_count(SOURCES[name])
            for name in (
                "ada_dry_run",
                "doge_link_dry_run",
                "protective_dry_run",
                "sl_quality_dry_run",
                "confidence_sl_dry_run",
            )
        )

        return {
            "agent_status": "ONLINE" if online else "OFFLINE",
            "last_cycle": latest_signal.get("timestamp", "N/A"),
            "total_analyzed": stats.get("analyzed_symbols", len(signals)),
            "open_trades": metrics["open"],
            "closed_trades": metrics["closed"],
            "winrate": metrics["winrate"],
            "profit_factor": metrics["profit_factor"],
            "current_market_regime": market.get("current_market", {}).get("regime", "N/A"),
            "best_opportunity": best_symbol,
            "near_setup_count": len(near_rows),
            "dry_run_candidates": dry_run_candidates,
            "opportunity_status": opportunity.get("status", "N/A"),
        }

    def build_hypotheses(self, status: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Collect main research hypotheses."""
        dataset = self.json_sources.get("dataset_quality", {})
        replay = self.json_sources.get("strategy_replay", {})
        opportunity = self.json_sources.get("opportunity_expansion", {})
        market = self.json_sources.get("market_regime", {})
        sl_quality = self.json_sources.get("sl_quality", {})
        confidence = self.json_sources.get("confidence_calibration", {})

        reliability = dataset.get("statistical_reliability", {})
        dry_run_quality = dataset.get("dry_run_quality", {})

        hypotheses = [
            {
                "name": "MIN_EDGE",
                "status": "KEEP",
                "confidence": reliability_level(reliability, "MIN_EDGE Analysis"),
                "evidence": "MIN_EDGE analysis has high observational coverage; opportunity expansion does not allow safe live relaxation.",
                "source": "dataset_quality_report.json / trade_opportunity_expansion_report.json",
            },
            {
                "name": "Momentum",
                "status": "KEEP",
                "confidence": reliability_level(reliability, "Momentum Analysis"),
                "evidence": "Momentum analysis has strong sample coverage and prior blocked/outcome checks did not justify weakening it.",
                "source": "dataset_quality_report.json",
            },
            {
                "name": "SL Quality D",
                "status": sl_quality.get("recommendation", {}).get("status", "OBSERVE"),
                "confidence": trade_sample_confidence(status),
                "evidence": best_candidate_text(sl_quality),
                "source": "sl_entry_quality_experiment_report.json",
            },
            {
                "name": "Confidence > 90 + SL D",
                "status": confidence.get("recommendation", {}).get("status", "OBSERVE"),
                "confidence": trade_sample_confidence(status),
                "evidence": best_candidate_text(confidence),
                "source": "confidence_calibration_experiment_report.json",
            },
            {
                "name": "DOGE/LINK Opportunity",
                "status": dry_run_status(SOURCES["doge_link_dry_run"], opportunity),
                "confidence": "LOW",
                "evidence": (
                    f"Dry-run candidates: {dry_run_count(SOURCES['doge_link_dry_run'])}; "
                    "нужно live-наблюдение outcome."
                ),
                "source": "doge_link_opportunity_dry_run.csv / trade_opportunity_expansion_report.json",
            },
            {
                "name": "ADA Opportunity",
                "status": dry_run_status(SOURCES["ada_dry_run"], opportunity),
                "confidence": "LOW",
                "evidence": (
                    f"Dry-run candidates: {dry_run_count(SOURCES['ada_dry_run'])}; "
                    "нужно live-наблюдение outcome."
                ),
                "source": "ada_opportunity_dry_run.csv / trade_opportunity_expansion_report.json",
            },
            {
                "name": "Market Regime",
                "status": market.get("status", "OBSERVE_ONLY"),
                "confidence": "MEDIUM" if market.get("current_market") else "LOW",
                "evidence": (
                    "Текущий режим: "
                    f"{market.get('current_market', {}).get('regime', 'N/A')}; "
                    "режимы помогают понять, где появляются Near Setup."
                ),
                "source": "market_regime_advisor_report.json",
            },
            {
                "name": "Strategy Replay",
                "status": replay.get("status", "INSUFFICIENT_DATA"),
                "confidence": replay.get("sample", {}).get("data_confidence", "LOW"),
                "evidence": replay.get("final_recommendation", {}).get(
                    "reason",
                    "Replay uses closed trades and remains observational.",
                ),
                "source": "strategy_replay_report.json",
            },
        ]

        return [normalize_hypothesis(item) for item in hypotheses]

    def proven_findings(self, hypotheses: list[Mapping[str, Any]]) -> list[str]:
        """Return concise proven findings."""
        findings = [
            "MIN_EDGE не стоит снижать вслепую.",
            "Momentum сейчас скорее защищает стратегию.",
            "Общие ослабления порогов опасны без replay/outcome подтверждения.",
            "Лучшие Near Setup кандидаты зависят от символа и рыночного режима.",
        ]
        dataset = self.json_sources.get("dataset_quality", {})
        reliability = dataset.get("statistical_reliability", {})
        if reliability.get("Trend Conflict Replay", {}).get("reliability") == "HIGH":
            findings.append("Trend/Momentum conflict имеет сильную observational-базу.")
        return findings

    def observation_items(self) -> list[str]:
        """Return items requiring more observation."""
        status = self.build_status()
        items = []
        if status.get("best_opportunity") not in {"N/A", "нет"}:
            items.append(f"{status['best_opportunity']} Near Setup.")
        items.extend(
            [
                "DOGE/LINK Near Setup.",
                "ADA Near Setup.",
                "SHORT + SL Quality D.",
                "Confidence >90 + SL Quality D.",
            ]
        )
        return items

    def forbidden_actions(self) -> list[str]:
        """Return actions that should not be taken now."""
        return [
            "Не снижать MIN_EDGE.",
            "Не снижать MIN_SCORE.",
            "Не снижать MIN_CONFIDENCE.",
            "Не менять DecisionEngine.",
            "Не включать 2+ сделки без отдельного PortfolioManager approval.",
            "Не внедрять защитные фильтры в live без 30-50 закрытых сделок.",
        ]

    def next_best_action(self, status: Mapping[str, Any]) -> str:
        """Choose one next best action."""
        if safe_float(status.get("closed_trades")) < 30:
            return "COLLECT_MORE_DATA"
        if status.get("dry_run_candidates", 0) > 0:
            return "RUN_DRY_RUN_OBSERVATION"
        return "READY_FOR_STRATEGY_CHANGE"

    def build_report(self) -> dict[str, Any]:
        """Build the full Research Hub report."""
        status = self.build_status()
        hypotheses = self.build_hypotheses(status)
        return {
            "generated_at": utc_now(),
            "mode": "READ_ONLY",
            "overall_status": status,
            "hypotheses": hypotheses,
            "proven": self.proven_findings(hypotheses),
            "observe": self.observation_items(),
            "do_not_do_now": self.forbidden_actions(),
            "next_best_action": self.next_best_action(status),
            "source_files": {
                name: file_state(path)
                for name, path in SOURCES.items()
            },
            "warnings": self.warnings,
            "notes": [
                "Research Hub only aggregates existing reports and CSV files.",
                "No live logic, strategy, config, weights, dry-run or replay code is changed.",
            ],
        }

    def save_outputs(self, report: Mapping[str, Any]) -> None:
        """Save JSON, TXT and CSV outputs."""
        with REPORT_FILE.open("w", encoding="utf-8") as file:
            json.dump(report, file, indent=2, ensure_ascii=False)
        SUMMARY_FILE.write_text(format_summary(report), encoding="utf-8")
        write_hypotheses_csv(report.get("hypotheses", []))

    def print_report(self, report: Mapping[str, Any]) -> None:
        """Print the short summary."""
        print(format_summary(report))


def reliability_level(
    reliability: Mapping[str, Any],
    key: str,
) -> str:
    """Return reliability label from dataset quality report."""
    value = reliability.get(key, {})
    return value.get("reliability", "LOW")


def trade_sample_confidence(status: Mapping[str, Any]) -> str:
    """Return confidence based on closed trade sample size."""
    closed = safe_float(status.get("closed_trades"))
    if closed >= 50:
        return "HIGH"
    if closed >= 30:
        return "MEDIUM"
    return "LOW"


def best_candidate_text(report: Mapping[str, Any]) -> str:
    """Format best candidate evidence from experiment reports."""
    candidate = report.get("best_candidate", {})
    recommendation = report.get("recommendation", {})
    if not candidate and not recommendation:
        return "Недостаточно данных."
    scenario = (
        recommendation.get("best_scenario")
        or recommendation.get("best_candidate")
        or candidate.get("scenario")
        or "N/A"
    )
    prevented_losses = candidate.get("prevented_losses", "N/A")
    lost_wins = candidate.get("lost_wins", "N/A")
    return (
        f"Лучший сценарий: {scenario}; "
        f"prevented_losses={prevented_losses}; lost_wins={lost_wins}; "
        f"apply_automatically={recommendation.get('apply_automatically', False)}."
    )


def dry_run_status(path: Path, opportunity_report: Mapping[str, Any]) -> str:
    """Return status for opportunity dry-runs."""
    candidates = dry_run_count(path)
    if candidates > 0:
        return "PROMISING_DRY_RUN"
    if opportunity_report.get("status") in {"PROMISING_DRY_RUN", "READY_FOR_BACKTEST"}:
        return "OBSERVE_ONLY"
    return "INSUFFICIENT_DATA"


def normalize_hypothesis(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize hypothesis status naming."""
    status = str(payload.get("status", "OBSERVE")).upper()
    if status == "CANDIDATE_FOR_DRY_RUN":
        status = "PROMISING_DRY_RUN"
    if status == "INSUFFICIENT_DATA":
        normalized = "INSUFFICIENT_DATA"
    elif status in {"KEEP", "OBSERVE", "OBSERVE_ONLY", "PROMISING", "PROMISING_DRY_RUN"}:
        normalized = status
    elif status in {"OK", "WARNING"}:
        normalized = "OBSERVE"
    else:
        normalized = status
    return {
        "name": payload.get("name", "N/A"),
        "status": normalized,
        "confidence": payload.get("confidence", "LOW"),
        "evidence": payload.get("evidence", ""),
        "source": payload.get("source", ""),
    }


def format_summary(report: Mapping[str, Any]) -> str:
    """Format short Russian summary."""
    status = report.get("overall_status", {})
    hypotheses = report.get("hypotheses", [])
    selected = [
        item for item in hypotheses
        if item.get("name") in {
            "MIN_EDGE",
            "Momentum",
            "SL Quality D",
            "DOGE/LINK Opportunity",
            "ADA Opportunity",
        }
    ]
    lines = [
        "====================================",
        "Research Hub v1",
        "====================================",
        "Общий статус:",
        f"Агент: {status.get('agent_status', 'N/A')}",
        f"Рынок: {status.get('current_market_regime', 'N/A')}",
        f"Лучший кандидат: {status.get('best_opportunity', 'N/A')}",
        f"Near Setup: {status.get('near_setup_count', 0)}",
        f"Закрытых сделок: {status.get('closed_trades', 0)}",
        f"Winrate: {safe_float(status.get('winrate')):.1f}%",
        f"Profit Factor: {safe_float(status.get('profit_factor')):.2f}",
        "",
        "Главные выводы:",
    ]
    for index, item in enumerate(selected, start=1):
        lines.append(f"{index}. {item.get('name')} — {item.get('status')}")
    lines.extend(["", "Что уже доказано:"])
    lines.extend(f"- {item}" for item in report.get("proven", [])[:5])
    lines.extend(["", "Требует наблюдения:"])
    lines.extend(f"- {item}" for item in report.get("observe", [])[:5])
    lines.extend(
        [
            "",
            "Что делать:",
            str(report.get("next_best_action", "N/A")),
            "",
            "Что не делать:",
        ]
    )
    lines.extend(f"- {item}" for item in report.get("do_not_do_now", [])[:5])
    warnings = report.get("warnings", [])
    if warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {item}" for item in warnings[:5])
    return "\n".join(lines)


def write_hypotheses_csv(hypotheses: list[Mapping[str, Any]]) -> None:
    """Write hypotheses CSV."""
    fieldnames = ["name", "status", "confidence", "evidence", "source"]
    with HYPOTHESES_FILE.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for item in hypotheses:
            writer.writerow({field: item.get(field, "") for field in fieldnames})


def main() -> None:
    """Run Research Hub."""
    hub = ResearchHub()
    report = hub.build_report()
    hub.save_outputs(report)
    hub.print_report(report)


if __name__ == "__main__":
    main()
