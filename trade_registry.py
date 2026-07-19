"""Read-only Single Source of Truth for the AITradingAgent trade lifecycle.

The registry owns trade loading, lifecycle classification, data-quality checks,
and research-facing selections.  It delegates R normalization to the existing
``trade_metrics_normalizer`` contract and never edits the source trade file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from trade_metrics_normalizer import (
    aggregate_trade_metrics,
    canonical_symbol,
    is_closed_trade,
    normalize_trade,
    optional_float,
    read_trade_rows,
)


BASE_DIR = Path(__file__).resolve().parent
TRADES_FILE = BASE_DIR / "trades.csv"
REPORT_DIR = BASE_DIR / "reports"
REGISTRY_REPORT = REPORT_DIR / "trade_registry.json"
QUALITY_REPORT = REPORT_DIR / "data_quality.json"
QUALITY_SUMMARY = REPORT_DIR / "data_quality_summary.txt"
HISTORY_DIR = REPORT_DIR / "data_quality_history"

LIFECYCLE_STATUSES = {"OPEN", "CLOSED", "COMPLETE", "INCOMPLETE", "INVALID", "ARCHIVED"}
SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
REQUIRED_FIELDS = (
    "symbol",
    "direction",
    "entry",
    "exit",
    "sl",
    "tp",
    "opened_at",
    "closed_at",
    "result",
    "R",
    "status",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _fingerprint(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _trade_id(row: Mapping[str, Any], source_index: int) -> str:
    explicit = _text(row.get("trade_id") or row.get("id"))
    if explicit:
        return explicit
    identity = "|".join(
        (
            canonical_symbol(row.get("symbol") or row.get("pair")),
            _text(row.get("direction") or row.get("side")).upper(),
            _text(row.get("opened_at") or row.get("open_timestamp") or row.get("timestamp")),
            str(source_index),
        )
    )
    return f"TR-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"


def _duplicate_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        canonical_symbol(row.get("symbol") or row.get("pair")),
        _text(row.get("direction") or row.get("side")).upper(),
        _text(row.get("opened_at") or row.get("open_timestamp") or row.get("timestamp")),
    )


def _issue(
    trade_id: str,
    severity: str,
    error_type: str,
    description: str,
) -> dict[str, str]:
    assert severity in SEVERITIES
    return {
        "trade_id": trade_id,
        "severity": severity,
        "error_type": error_type,
        "description": description,
    }


def _canonical_record(row: Mapping[str, Any], source_index: int) -> dict[str, Any]:
    """Build one API-compatible canonical record without mutating its source."""
    raw = dict(row)
    normalized = normalize_trade(raw, source_index)
    trade_id = _trade_id(raw, source_index)
    source_status = _text(raw.get("status") or raw.get("state")).upper()
    result = _text(normalized.get("result") or raw.get("result")).upper()
    closed = is_closed_trade(raw)
    archived = source_status == "ARCHIVED" or _text(raw.get("archived")).lower() in {"1", "true", "yes"}
    record = {
        **raw,
        "trade_id": trade_id,
        "source_index": source_index,
        "symbol": normalized.get("symbol", ""),
        "direction": normalized.get("direction", ""),
        "entry": normalized.get("entry", ""),
        "exit": normalized.get("exit_price", ""),
        "exit_price": normalized.get("exit_price", ""),
        "sl": normalized.get("stop_loss", ""),
        "stop_loss": normalized.get("stop_loss", ""),
        "tp": normalized.get("take_profit", ""),
        "take_profit": normalized.get("take_profit", ""),
        "opened_at": normalized.get("opened_at", ""),
        "closed_at": normalized.get("closed_at", ""),
        "result": result,
        "R": normalized.get("pnl_r", ""),
        "r": normalized.get("pnl_r", ""),
        "pnl_r": normalized.get("pnl_r", ""),
        "pnl_percent": normalized.get("pnl_percent", ""),
        "risk_per_unit": normalized.get("risk_per_unit", ""),
        "metrics_status": normalized.get("metrics_status", "INCOMPLETE"),
        "result_mismatch": bool(normalized.get("result_mismatch")),
        "source_status": source_status,
        "source_result": normalized.get("source_result", ""),
        "is_closed": closed,
        "archived": archived,
    }
    return record


def validate_trade(
    trade: Mapping[str, Any],
    *,
    duplicate: bool = False,
) -> list[dict[str, str]]:
    """Validate one canonical or raw trade and return structured issues."""
    source_index = int(float(trade.get("source_index") or 0))
    record = (
        dict(trade)
        if "trade_id" in trade and "R" in trade and "sl" in trade
        else _canonical_record(trade, source_index)
    )
    trade_id = _text(record.get("trade_id")) or _trade_id(record, source_index)
    issues: list[dict[str, str]] = []
    closed = bool(record.get("is_closed")) or (
        _text(record.get("result")).upper() in {"WIN", "LOSS"}
        and _text(record.get("closed_at"))
    )
    if duplicate:
        issues.append(_issue(trade_id, "HIGH", "duplicate_trade", "Duplicate symbol/direction/opened_at identity."))

    direction = _text(record.get("direction")).upper()
    symbol = canonical_symbol(record.get("symbol"))
    if not symbol or direction not in {"LONG", "SHORT"}:
        issues.append(_issue(trade_id, "CRITICAL", "missing_fields", "symbol or direction is missing/invalid."))

    required = REQUIRED_FIELDS if closed else ("symbol", "direction", "entry", "sl", "tp", "opened_at", "status")
    missing = [name for name in required if record.get(name) is None or _text(record.get(name)) == ""]
    if missing:
        issues.append(_issue(trade_id, "HIGH" if closed else "MEDIUM", "missing_fields", f"Missing required fields: {', '.join(missing)}."))

    prices: dict[str, float | None] = {
        name: optional_float(record.get(name)) for name in ("entry", "exit", "sl", "tp")
    }
    populated_prices = [value for value in prices.values() if value is not None]
    if any(value <= 0 or not math.isfinite(value) for value in populated_prices):
        issues.append(_issue(trade_id, "CRITICAL", "invalid_price", "Price fields must be finite and greater than zero."))
    entry, stop = prices["entry"], prices["sl"]
    if entry is not None and stop is not None and math.isclose(entry, stop, rel_tol=0, abs_tol=1e-12):
        issues.append(_issue(trade_id, "CRITICAL", "zero_risk", "Entry equals stop loss; R cannot be defined."))

    opened = _parse_time(record.get("opened_at"))
    closed_at = _parse_time(record.get("closed_at"))
    if _text(record.get("opened_at")) and opened is None:
        issues.append(_issue(trade_id, "HIGH", "time_order_error", "opened_at is not a valid ISO timestamp."))
    if _text(record.get("closed_at")) and closed_at is None:
        issues.append(_issue(trade_id, "HIGH", "time_order_error", "closed_at is not a valid ISO timestamp."))
    if opened and closed_at and closed_at < opened:
        issues.append(_issue(trade_id, "HIGH", "negative_duration", "closed_at is earlier than opened_at."))
        issues.append(_issue(trade_id, "HIGH", "time_order_error", "Trade timestamps are out of chronological order."))

    supplied_rr = optional_float(record.get("rr") or record.get("RR"))
    tp = prices["tp"]
    if supplied_rr is not None and entry is not None and stop is not None and tp is not None:
        risk = abs(entry - stop)
        calculated_rr = abs(tp - entry) / risk if risk > 0 else None
        if calculated_rr is not None and not math.isclose(supplied_rr, calculated_rr, rel_tol=0.05, abs_tol=0.05):
            issues.append(_issue(trade_id, "MEDIUM", "wrong_rr", f"Recorded RR={supplied_rr:g}, calculated RR={calculated_rr:.4f}."))
    return issues


def _classify(record: Mapping[str, Any], issues: Sequence[Mapping[str, Any]]) -> str:
    if record.get("archived"):
        return "ARCHIVED"
    critical = any(row.get("severity") == "CRITICAL" for row in issues)
    corrupt_time = any(row.get("error_type") in {"negative_duration", "time_order_error"} for row in issues)
    duplicate = any(row.get("error_type") == "duplicate_trade" for row in issues)
    if critical or corrupt_time or duplicate:
        return "INVALID"
    if not record.get("is_closed"):
        return "OPEN"
    missing = any(row.get("error_type") == "missing_fields" for row in issues)
    if missing or record.get("metrics_status") != "COMPLETE":
        return "INCOMPLETE"
    required_present = all(record.get(name) is not None and _text(record.get(name)) for name in REQUIRED_FIELDS if name != "status")
    return "COMPLETE" if required_present else "CLOSED"


class TradeRegistry:
    """Immutable read view over one trade source file or supplied row set."""

    def __init__(
        self,
        path: Path | str = TRADES_FILE,
        *,
        rows: Iterable[Mapping[str, Any]] | None = None,
    ) -> None:
        self.path = Path(path)
        source_rows = list(rows) if rows is not None else read_trade_rows(self.path)
        duplicate_counts = Counter(
            key for key in (_duplicate_key(row) for row in source_rows) if all(key)
        )
        seen: Counter[tuple[str, str, str]] = Counter()
        self._trades: list[dict[str, Any]] = []
        self._issues: list[dict[str, str]] = []
        for index, source in enumerate(source_rows, start=1):
            record = _canonical_record(source, index)
            key = _duplicate_key(source)
            seen[key] += 1
            duplicate = bool(all(key) and duplicate_counts[key] > 1 and seen[key] > 1)
            issues = validate_trade(record, duplicate=duplicate)
            status = _classify(record, issues)
            record["status"] = status
            record["registry_status"] = status
            record["quality_issues"] = [row["error_type"] for row in issues]
            record["complete"] = status == "COMPLETE"
            self._trades.append(record)
            self._issues.extend(issues)
        self._by_id = {row["trade_id"]: row for row in self._trades}
        self.dataset_fingerprint = _fingerprint(
            [
                {
                    "trade_id": row["trade_id"],
                    "status": row["status"],
                    "symbol": row["symbol"],
                    "direction": row["direction"],
                    "opened_at": row["opened_at"],
                    "closed_at": row["closed_at"],
                    "R": row["R"],
                    "issues": row["quality_issues"],
                }
                for row in self._trades
            ]
        )

    def get_all_trades(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._trades]

    def get_complete_trades(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._trades if row["status"] == "COMPLETE"]

    def get_closed_trades(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._trades if row.get("is_closed") and row["status"] != "ARCHIVED"]

    def get_open_trades(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._trades if row["status"] == "OPEN"]

    def get_trade(self, trade_id: str) -> dict[str, Any] | None:
        row = self._by_id.get(str(trade_id))
        return dict(row) if row else None

    def get_issues(self) -> list[dict[str, str]]:
        return [dict(row) for row in self._issues]

    def get_statistics(self) -> dict[str, Any]:
        counts = Counter(row["status"] for row in self._trades)
        closed = sum(bool(row.get("is_closed")) and row["status"] != "ARCHIVED" for row in self._trades)
        complete = counts["COMPLETE"]
        return {
            "total_trades": len(self._trades),
            "open_trades": counts["OPEN"],
            "closed_trades": closed,
            "complete_trades": complete,
            "incomplete_trades": counts["INCOMPLETE"] + counts["CLOSED"],
            "invalid_trades": counts["INVALID"],
            "archived_trades": counts["ARCHIVED"],
            "completion_percent": round(complete / closed * 100, 2) if closed else 0.0,
        }

    def get_metrics(self) -> dict[str, Any]:
        """Return existing unified R metrics over the registry COMPLETE set."""
        # aggregate_trade_metrics reuses the exact established formulas.  All
        # rows here are already COMPLETE, so every consumer gets one sample.
        return aggregate_trade_metrics(self.get_complete_trades())

    def build_reports(self) -> tuple[dict[str, Any], dict[str, Any]]:
        generated_at = datetime.now(timezone.utc).isoformat()
        statistics = self.get_statistics()
        metrics = self.get_metrics()
        issue_counts = Counter(row["error_type"] for row in self._issues)
        severity_counts = Counter(row["severity"] for row in self._issues)
        health = (
            "CRITICAL"
            if severity_counts["CRITICAL"]
            else "POOR"
            if severity_counts["HIGH"] or statistics["completion_percent"] < 80
            else "WARNING"
            if self._issues or statistics["completion_percent"] < 95
            else "GOOD"
        )
        registry_report = {
            "generated_at": generated_at,
            "mode": "READ_ONLY_TRADE_REGISTRY",
            "status": "OK" if health in {"GOOD", "WARNING"} else "WARNING",
            "source": str(self.path),
            "dataset_fingerprint": self.dataset_fingerprint,
            "statistics": statistics,
            "metrics": metrics,
            "trades": self.get_all_trades(),
            "api": [
                "get_all_trades",
                "get_complete_trades",
                "get_closed_trades",
                "get_open_trades",
                "get_trade",
                "get_statistics",
                "validate_trade",
            ],
            "restrictions": {
                "read_only": True,
                "automatic_application": False,
                "live_unchanged": True,
                "trading_logic_unchanged": True,
            },
        }
        quality_report = {
            "generated_at": generated_at,
            "mode": "READ_ONLY_DATA_QUALITY",
            "status": health,
            "dataset_fingerprint": self.dataset_fingerprint,
            "statistics": statistics,
            "issue_counts": dict(sorted(issue_counts.items())),
            "severity_counts": {key: severity_counts[key] for key in ("CRITICAL", "HIGH", "MEDIUM", "LOW")},
            "issues": self.get_issues(),
            "recommendation": (
                "Fix incomplete or invalid trades before promoting hypotheses."
                if self._issues or statistics["incomplete_trades"] or statistics["invalid_trades"]
                else "Data is ready for continued Shadow Research."
            ),
            "restrictions": registry_report["restrictions"],
        }
        return registry_report, quality_report


def get_all_trades(path: Path | str = TRADES_FILE) -> list[dict[str, Any]]:
    return TradeRegistry(path).get_all_trades()


def get_complete_trades(path: Path | str = TRADES_FILE) -> list[dict[str, Any]]:
    return TradeRegistry(path).get_complete_trades()


def get_closed_trades(path: Path | str = TRADES_FILE) -> list[dict[str, Any]]:
    return TradeRegistry(path).get_closed_trades()


def get_open_trades(path: Path | str = TRADES_FILE) -> list[dict[str, Any]]:
    return TradeRegistry(path).get_open_trades()


def get_trade(trade_id: str, path: Path | str = TRADES_FILE) -> dict[str, Any] | None:
    return TradeRegistry(path).get_trade(trade_id)


def get_statistics(path: Path | str = TRADES_FILE) -> dict[str, Any]:
    return TradeRegistry(path).get_statistics()


def format_summary(report: Mapping[str, Any]) -> str:
    stats = report.get("statistics", {})
    issue_counts = report.get("issue_counts", {})
    lines = [
        "📋 Data Quality",
        f"Total trades: {stats.get('total_trades', 0)}",
        f"Open: {stats.get('open_trades', 0)}",
        f"Closed: {stats.get('closed_trades', 0)}",
        f"Complete: {stats.get('complete_trades', 0)}",
        f"Incomplete: {stats.get('incomplete_trades', 0)}",
        f"Invalid: {stats.get('invalid_trades', 0)}",
        f"Archived: {stats.get('archived_trades', 0)}",
        f"Completion: {stats.get('completion_percent', 0)}%",
        "Issues:",
    ]
    if issue_counts:
        lines.extend(f"• {name}: {count}" for name, count in issue_counts.items())
    else:
        lines.append("• None")
    lines.extend(
        [
            "Overall Health:",
            str(report.get("status", "NOT_AVAILABLE")),
            "Recommendation:",
            str(report.get("recommendation", "Run trade_registry.py")),
        ]
    )
    return "\n".join(lines)


def save_reports(
    registry_report: Mapping[str, Any],
    quality_report: Mapping[str, Any],
    *,
    registry_path: Path = REGISTRY_REPORT,
    quality_path: Path = QUALITY_REPORT,
    summary_path: Path = QUALITY_SUMMARY,
    history_dir: Path = HISTORY_DIR,
) -> Path | None:
    _atomic_write(registry_path, json.dumps(registry_report, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(quality_path, json.dumps(quality_report, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(summary_path, format_summary(quality_report) + "\n")
    fingerprint = str(quality_report.get("dataset_fingerprint", ""))
    history_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(history_dir.glob("*.json"), reverse=True):
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if previous.get("dataset_fingerprint") == fingerprint:
            return None
        break
    generated = _parse_time(quality_report.get("generated_at")) or datetime.now(timezone.utc)
    snapshot = history_dir / generated.strftime("%Y-%m-%d_%H-%M-%S.json")
    _atomic_write(snapshot, json.dumps(quality_report, ensure_ascii=False, indent=2) + "\n")
    return snapshot


def read_quality_report(path: Path = QUALITY_REPORT) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades", type=Path, default=TRADES_FILE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    registry = TradeRegistry(args.trades)
    registry_report, quality_report = registry.build_reports()
    snapshot = save_reports(registry_report, quality_report)
    print(format_summary(quality_report))
    print(f"History snapshot: {snapshot.name if snapshot else 'deduplicated'}")


if __name__ == "__main__":
    main()
