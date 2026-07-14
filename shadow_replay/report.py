"""Atomic report writers and human-readable output for Shadow Replay v2."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping


CSV_FIELDS = (
    "source_index",
    "symbol",
    "direction",
    "opened_at",
    "closed_at",
    "ideal_entry",
    "effective_entry",
    "ideal_exit",
    "effective_exit",
    "latency_seconds",
    "ideal_gross_r",
    "effective_gross_r",
    "total_fee_r",
    "funding_r",
    "slippage_impact_r",
    "latency_impact_r",
    "effective_net_r",
    "execution_quality",
    "portfolio_allowed",
    "portfolio_status",
    "portfolio_reasons",
    "correlation_warnings",
    "capital_used",
    "risk_used_pct",
    "available_margin",
)


def _atomic_path(path: Path) -> tuple[NamedTemporaryFile, Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    return temporary, Path(temporary.name)


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """Replace a JSON report only after the complete payload is written."""
    temporary, temp_path = _atomic_path(path)
    try:
        with temporary:
            json.dump(payload, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_text_atomic(path: Path, text: str) -> None:
    """Atomically replace a UTF-8 text report."""
    temporary, temp_path = _atomic_path(path)
    try:
        with temporary:
            temporary.write(text.rstrip() + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_csv_atomic(path: Path, rows: list[Mapping[str, Any]]) -> None:
    """Atomically replace the per-trade research table."""
    temporary, temp_path = _atomic_path(path)
    try:
        with temporary:
            writer = csv.DictWriter(temporary, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for row in rows:
                prepared = {field: row.get(field, "") for field in CSV_FIELDS}
                for field in ("portfolio_reasons", "correlation_warnings"):
                    if isinstance(prepared[field], (list, dict)):
                        prepared[field] = json.dumps(
                            prepared[field], ensure_ascii=False, separators=(",", ":")
                        )
                writer.writerow(prepared)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def format_summary(report: Mapping[str, Any]) -> str:
    """Format a compact Russian research summary."""
    sample = report.get("sample", {})
    metrics = report.get("metrics", {})
    ideal = metrics.get("ideal_all", {})
    real = metrics.get("effective_portfolio", {})
    impact = metrics.get("impact", {})
    execution = report.get("execution_quality", {})
    portfolio = report.get("portfolio", {})
    lines = [
        "====================================",
        "Shadow Replay Engine v2",
        "====================================",
        f"Статус: {report.get('status', 'INSUFFICIENT_DATA')}",
        f"Закрытых сделок: {sample.get('closed_trades_total', 0)}",
        f"Полных R-метрик: {sample.get('complete_metrics_total', 0)}",
        f"Replay-сделок: {real.get('trades', 0)}",
        "",
        "Идеальная модель:",
        f"Profit Factor: {ideal.get('profit_factor', 0):.4f}",
        f"Net R: {ideal.get('net_r', 0):+.4f}",
        "",
        "Реалистичная модель:",
        f"Profit Factor: {real.get('profit_factor', 0):.4f}",
        f"Net R: {real.get('net_r', 0):+.4f}",
        f"Winrate: {real.get('winrate', 0):.2f}%",
        f"Max Drawdown: {real.get('max_drawdown_r', 0):.4f} R",
        "",
        "Влияние исполнения:",
        f"Комиссии: {impact.get('fee_impact_r', 0):+.4f} R",
        f"Slippage: {impact.get('slippage_impact_r', 0):+.4f} R",
        f"Funding: {impact.get('funding_impact_r', 0):+.4f} R",
        f"Latency: {impact.get('latency_impact_r', 0):+.4f} R",
        f"Средняя задержка: {execution.get('average_delay_seconds', 0):.2f} сек",
        f"Качество исполнения: {execution.get('status', 'N/A')}",
        "",
        "Portfolio Replay:",
        f"Eligible: {portfolio.get('portfolio_eligible_trades', 0)}",
        (
            "Исключено из overlap simulation: "
            f"{portfolio.get('portfolio_time_incomplete', 0)}"
        ),
        f"Исполнено: {portfolio.get('trades_executed', 0)}",
        f"Пропущено: {portfolio.get('trades_skipped', 0)}",
        f"Correlation warnings: {portfolio.get('correlation_warning_count', 0)}",
        "",
        f"Вывод: {report.get('recommendation', '')}",
        "LIVE-логика не изменялась.",
    ]
    return "\n".join(lines)
