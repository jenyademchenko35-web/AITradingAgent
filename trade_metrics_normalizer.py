"""Unified read-only trade metrics for AITradingAgent analytics.

Raw ``pnl`` values in the historical trade file use mixed units. This module
therefore derives all performance metrics from entry, exit and stop prices. It
never edits ``trades.csv`` and has no connection to live trade execution.
"""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping

from report_metadata import build_report_metadata, timestamp_bounds


BASE_DIR = Path(__file__).resolve().parent
TRADES_FILE = BASE_DIR / "trades.csv"
AUDIT_JSON = BASE_DIR / "trade_metrics_audit.json"
AUDIT_SUMMARY = BASE_DIR / "trade_metrics_audit_summary.txt"
NORMALIZED_CSV = BASE_DIR / "normalized_trade_metrics.csv"

CLOSED_MARKERS = {"WIN", "LOSS", "CLOSED", "TP", "SL"}
OPEN_MARKERS = {"OPEN", "ACTIVE", "PENDING"}

NORMALIZED_FIELDS = [
    "trade_id",
    "source_row_id",
    "source_index",
    "symbol",
    "direction",
    "opened_at",
    "closed_at",
    "entry",
    "stop_loss",
    "take_profit",
    "exit_price",
    "risk_per_unit",
    "pnl_percent",
    "pnl_r",
    "result",
    "source_status",
    "source_result",
    "metrics_status",
    "metrics_incomplete",
    "incomplete_reasons",
    "result_mismatch",
    "raw_pnl",
    "raw_pnl_unit",
]


def utc_now() -> str:
    """Return an ISO UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def optional_float(value: Any) -> float | None:
    """Convert a populated value to float and preserve missing values."""
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = float(str(value).replace("%", "").strip())
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def first_value(row: Mapping[str, Any], names: Iterable[str]) -> Any:
    """Return the first populated alias from a row."""
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip() != "":
            return value
    return ""


def source_result(row: Mapping[str, Any]) -> str:
    """Return a normalized recorded result/status when available."""
    result = str(row.get("result") or "").strip().upper()
    status = str(row.get("status") or row.get("state") or "").strip().upper()
    if result in {"WIN", "LOSS"}:
        return result
    if status in {"WIN", "TP"}:
        return "WIN"
    if status in {"LOSS", "SL"}:
        return "LOSS"
    return result


def canonical_symbol(value: Any) -> str:
    """Normalize a symbol for stable analytics-only joins."""
    text = str(value or "").strip().upper().replace("-", "/")
    if "/" not in text and text.endswith("USDT"):
        text = f"{text[:-4]}/USDT"
    return text


def canonical_timestamp(value: Any) -> str:
    """Normalize an ISO timestamp while preserving unparseable legacy values."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat()


def trade_identity_keys(
    row: Mapping[str, Any],
    source_index: int = 0,
) -> list[tuple[str, tuple[str, ...]]]:
    """Return stable linkage keys ordered from strongest to legacy fallback."""
    keys: list[tuple[str, tuple[str, ...]]] = []
    trade_id = str(first_value(row, ("trade_id", "id"))).strip()
    source_row_id = str(
        first_value(row, ("source_row_id", "row_id", "source_index"))
        or source_index
        or ""
    ).strip()
    symbol = canonical_symbol(first_value(row, ("symbol", "pair")))
    direction = str(first_value(row, ("direction", "side"))).strip().upper()
    opened_at = canonical_timestamp(
        first_value(row, ("opened_at", "open_timestamp", "timestamp"))
    )
    closed_at = canonical_timestamp(
        first_value(row, ("closed_at", "close_timestamp"))
    )

    if trade_id:
        keys.append(("TRADE_ID", (trade_id,)))
    if source_row_id:
        keys.append(("SOURCE_ROW_ID", (source_row_id,)))
    if symbol and direction and opened_at and closed_at:
        keys.append(
            ("SYMBOL_DIRECTION_OPEN_CLOSE", (symbol, direction, opened_at, closed_at))
        )
    if symbol and direction and opened_at:
        keys.append(("LEGACY_FALLBACK", (symbol, direction, opened_at)))
    return keys


def is_closed_trade(row: Mapping[str, Any]) -> bool:
    """Return whether a row represents a closed trade."""
    status = str(row.get("status") or row.get("state") or "").strip().upper()
    result = str(row.get("result") or "").strip().upper()
    if status in OPEN_MARKERS:
        return False
    if status in CLOSED_MARKERS or result in {"WIN", "LOSS"}:
        return True
    exit_price = first_value(row, ("exit_price", "exit"))
    closed_at = first_value(row, ("closed_at", "close_timestamp"))
    return bool(str(exit_price).strip() and str(closed_at).strip())


def normalize_trade(
    row: Mapping[str, Any],
    source_index: int = 0,
) -> dict[str, Any]:
    """Derive direction-aware percent and R metrics for one closed trade."""
    direction = str(row.get("direction") or row.get("side") or "").strip().upper()
    entry = optional_float(first_value(row, ("entry", "entry_price")))
    stop_loss = optional_float(first_value(row, ("stop_loss", "sl")))
    take_profit = optional_float(first_value(row, ("take_profit", "tp")))
    exit_price = optional_float(first_value(row, ("exit_price", "exit")))
    recorded_result = source_result(row)
    status = str(row.get("status") or row.get("state") or "").strip().upper()
    raw_pnl = first_value(row, ("pnl", "profit", "profit_loss"))
    trade_id = str(first_value(row, ("trade_id", "id"))).strip()
    source_row_id = str(
        first_value(row, ("source_row_id", "row_id", "source_index"))
        or source_index
        or ""
    ).strip()

    reasons: list[str] = []
    if direction not in {"LONG", "SHORT"}:
        reasons.append("direction отсутствует или некорректен")
    if entry is None or entry <= 0:
        reasons.append("entry отсутствует или некорректен")
    if exit_price is None or exit_price <= 0:
        reasons.append("exit_price отсутствует или некорректен")
    if stop_loss is None or stop_loss <= 0:
        reasons.append("stop_loss отсутствует или некорректен")

    risk_per_unit: float | None = None
    if entry is not None and stop_loss is not None and direction in {"LONG", "SHORT"}:
        risk_per_unit = (
            entry - stop_loss
            if direction == "LONG"
            else stop_loss - entry
        )
        if risk_per_unit <= 0:
            reasons.append("risk_per_unit не положительный")
            risk_per_unit = None

    pnl_percent: float | None = None
    pnl_r: float | None = None
    calculated_result = recorded_result
    if (
        direction in {"LONG", "SHORT"}
        and entry is not None
        and entry > 0
        and exit_price is not None
        and exit_price > 0
    ):
        price_change = (
            exit_price - entry
            if direction == "LONG"
            else entry - exit_price
        )
        pnl_percent = price_change / entry * 100
        if risk_per_unit is not None:
            pnl_r = price_change / risk_per_unit
        if price_change > 0:
            calculated_result = "WIN"
        elif price_change < 0:
            calculated_result = "LOSS"
        else:
            calculated_result = "BREAK_EVEN"

    complete = pnl_percent is not None and pnl_r is not None
    mismatch = bool(
        complete
        and recorded_result in {"WIN", "LOSS"}
        and calculated_result != recorded_result
    )
    return {
        "trade_id": trade_id,
        "source_row_id": source_row_id,
        "source_index": source_index,
        "symbol": canonical_symbol(row.get("symbol") or row.get("pair")),
        "direction": direction,
        "opened_at": first_value(row, ("opened_at", "open_timestamp", "timestamp")),
        "closed_at": first_value(row, ("closed_at", "close_timestamp")),
        "entry": entry if entry is not None else "",
        "stop_loss": stop_loss if stop_loss is not None else "",
        "take_profit": take_profit if take_profit is not None else "",
        "exit_price": exit_price if exit_price is not None else "",
        "risk_per_unit": round(risk_per_unit, 10) if risk_per_unit is not None else "",
        "pnl_percent": round(pnl_percent, 8) if pnl_percent is not None else "",
        "pnl_r": round(pnl_r, 8) if pnl_r is not None else "",
        "result": calculated_result,
        "source_status": status,
        "source_result": recorded_result,
        "metrics_status": "COMPLETE" if complete else "INCOMPLETE",
        "metrics_incomplete": not complete,
        "incomplete_reasons": " | ".join(reasons),
        "result_mismatch": mismatch,
        "raw_pnl": raw_pnl,
        "raw_pnl_unit": "UNKNOWN_IGNORED" if str(raw_pnl).strip() else "MISSING",
    }


def normalize_closed_trades(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize all closed trade rows while preserving source order."""
    return [
        normalize_trade(row, index)
        for index, row in enumerate(rows, start=1)
        if is_closed_trade(row)
    ]


def profit_factor_from_r(values: Iterable[float]) -> float:
    """Return Profit Factor from a sequence measured only in R."""
    returns = [float(value) for value in values]
    gross_profit = sum(value for value in returns if value > 0)
    gross_loss = abs(sum(value for value in returns if value < 0))
    if gross_loss <= 0:
        return 0.0
    return round(gross_profit / gross_loss, 6)


def max_drawdown_r(values: Iterable[float]) -> float:
    """Return maximum peak-to-trough drawdown on a cumulative R curve."""
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        equity += float(value)
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return round(drawdown, 6)


def aggregate_trade_metrics(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Calculate unified performance metrics from raw or normalized rows."""
    normalized = normalize_closed_trades(rows)
    complete = [row for row in normalized if row.get("metrics_status") == "COMPLETE"]
    complete.sort(
        key=lambda row: (
            str(row.get("closed_at") or row.get("opened_at") or ""),
            int(row.get("source_index") or 0),
        )
    )
    pnl_r_values = [float(row["pnl_r"]) for row in complete]
    pnl_percent_values = [float(row["pnl_percent"]) for row in complete]
    wins = [row for row in complete if row.get("result") == "WIN"]
    losses = [row for row in complete if row.get("result") == "LOSS"]
    return {
        "closed_trades": len(normalized),
        "metrics_trades": len(complete),
        "incomplete_metrics": len(normalized) - len(complete),
        "wins": len(wins),
        "losses": len(losses),
        "break_even": len(complete) - len(wins) - len(losses),
        "winrate": round(len(wins) / len(complete) * 100, 2) if complete else 0.0,
        "profit_factor": profit_factor_from_r(pnl_r_values),
        "net_r": round(sum(pnl_r_values), 6),
        "average_r": round(mean(pnl_r_values), 6) if pnl_r_values else 0.0,
        "average_pnl_percent": (
            round(mean(pnl_percent_values), 6) if pnl_percent_values else 0.0
        ),
        "max_drawdown_r": max_drawdown_r(pnl_r_values),
        "gross_profit_r": round(sum(value for value in pnl_r_values if value > 0), 6),
        "gross_loss_r": round(abs(sum(value for value in pnl_r_values if value < 0)), 6),
        "result_mismatches": sum(bool(row.get("result_mismatch")) for row in complete),
        "metric_unit": "R",
        "method": "entry_exit_stop_direction_normalization",
    }


def read_trade_rows(path: Path = TRADES_FILE) -> list[dict[str, str]]:
    """Read trade rows without mutating their source."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with path.open("r", newline="", encoding="utf-8") as file:
            return [dict(row) for row in csv.DictReader(file) if row and any(row.values())]
    except (OSError, csv.Error, UnicodeDecodeError):
        return []


def write_csv(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    """Write normalized rows to a separate audit artifact."""
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=NORMALIZED_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in NORMALIZED_FIELDS})


def build_audit_report(path: Path = TRADES_FILE) -> dict[str, Any]:
    """Build and persist the unified trade metrics audit."""
    source_rows = read_trade_rows(path)
    normalized = normalize_closed_trades(source_rows)
    metrics = aggregate_trade_metrics(source_rows)
    incomplete = [row for row in normalized if row.get("metrics_status") == "INCOMPLETE"]
    mismatches = [row for row in normalized if row.get("result_mismatch")]
    generated_at = utc_now()
    period_start, period_end = timestamp_bounds(
        normalized,
        fields=("opened_at", "closed_at"),
    )
    warnings = []
    if incomplete:
        warnings.append(
            f"{len(incomplete)} закрытых сделок исключены из PF, Net R и Drawdown."
        )
    if mismatches:
        warnings.append(
            f"{len(mismatches)} сделок имеют расхождение записанного и рассчитанного result."
        )
    report = {
        "generated_at": generated_at,
        "metadata": build_report_metadata(
            generator="trade_metrics_normalizer",
            metric_unit="R",
            source_files=[path],
            base_dir=BASE_DIR,
            data_period_start=period_start,
            data_period_end=period_end,
            closed_trades_total=metrics.get("closed_trades", 0),
            complete_metrics_total=metrics.get("metrics_trades", 0),
            generated_at=generated_at,
        ),
        "status": "WARNING" if incomplete or mismatches else "OK",
        "mode": "read-only unified trade metrics audit",
        "source": str(path),
        "source_rows": len(source_rows),
        "metrics": metrics,
        "data_quality": {
            "incomplete_count": len(incomplete),
            "result_mismatch_count": len(mismatches),
            "incomplete_trades": [
                {
                    "source_index": row.get("source_index"),
                    "symbol": row.get("symbol"),
                    "direction": row.get("direction"),
                    "source_result": row.get("source_result"),
                    "opened_at": row.get("opened_at"),
                    "reasons": row.get("incomplete_reasons"),
                }
                for row in incomplete
            ],
        },
        "methodology": {
            "pnl_percent_long": "(exit - entry) / entry * 100",
            "pnl_percent_short": "(entry - exit) / entry * 100",
            "risk_long": "entry - stop_loss",
            "risk_short": "stop_loss - entry",
            "pnl_r_long": "(exit - entry) / risk_per_unit",
            "pnl_r_short": "(entry - exit) / risk_per_unit",
            "profit_factor": "sum(positive pnl_r) / abs(sum(negative pnl_r))",
            "net_r": "sum(pnl_r); this is not percentage ROI",
            "max_drawdown_r": "maximum peak-to-trough decline of cumulative pnl_r",
            "raw_pnl": "ignored because its historical unit is unknown/mixed",
        },
        "warnings": warnings,
        "restrictions": [
            "trades.csv не изменялся.",
            "Расчёты не влияют на DecisionEngine или торговую логику.",
        ],
    }
    with AUDIT_JSON.open("w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    write_csv(NORMALIZED_CSV, normalized)
    AUDIT_SUMMARY.write_text(format_summary(report), encoding="utf-8")
    return report


def format_summary(report: Mapping[str, Any]) -> str:
    """Format the audit as a concise Russian text report."""
    metrics = report.get("metrics", {})
    lines = [
        "====================================",
        "Unified Trade Metrics Audit v1",
        "====================================",
        f"Статус: {report.get('status', 'N/A')}",
        f"Закрытых сделок: {metrics.get('closed_trades', 0)}",
        f"С полными метриками: {metrics.get('metrics_trades', 0)}",
        f"Incomplete metrics: {metrics.get('incomplete_metrics', 0)}",
        f"WIN: {metrics.get('wins', 0)}",
        f"LOSS: {metrics.get('losses', 0)}",
        f"Winrate: {metrics.get('winrate', 0)}%",
        f"Profit Factor: {metrics.get('profit_factor', 0)}",
        f"Net R: {metrics.get('net_r', 0)}",
        f"Max Drawdown: {metrics.get('max_drawdown_r', 0)} R",
        "",
        "Методика:",
        "- PnL рассчитывается только из entry/exit/direction.",
        "- Риск рассчитывается только из entry/stop_loss/direction.",
        "- Сырой pnl игнорируется из-за смешанных единиц.",
        "- Net R не является процентным ROI.",
    ]
    if report.get("warnings"):
        lines.extend(["", "Предупреждения:"])
        lines.extend(f"- {warning}" for warning in report.get("warnings", []))
    return "\n".join(lines)


def print_report(report: Mapping[str, Any]) -> None:
    """Print a human-readable audit report."""
    print(format_summary(report))


def main() -> None:
    """CLI entry point."""
    report = build_audit_report()
    print_report(report)


if __name__ == "__main__":
    main()
