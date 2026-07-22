"""Research-only data validation, recovery, coverage and decision snapshots.

The module never changes signal calculation or trading parameters. Historical
backfill is written to a derived JSON artifact; the legacy LIVE trades CSV is
kept byte-for-byte untouched.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from trade_registry import TradeRegistry

BASE_DIR = Path(__file__).resolve().parent
REPORT_PATH = BASE_DIR / "reports/data_quality.json"
BACKFILL_PATH = BASE_DIR / "reports/backfill_report.json"
ENRICHED_PATH = BASE_DIR / "reports/research_trades_enriched.json"
SNAPSHOT_PATH = BASE_DIR / "decision_snapshot.json"

RESEARCH_FIELDS = (
    "symbol", "direction", "timeframe", "market_regime", "trend_alignment",
    "volatility", "confidence", "score", "quality", "primary_blocker",
    "entry_price", "exit_price", "risk_reward", "result", "trade_id", "timestamp",
)
BACKFILL_FIELDS = (
    "confidence", "score", "quality", "trend_alignment", "volatility",
    "market_regime", "timeframe",
)
UNKNOWN_VALUES = {"", "UNKNOWN", "N/A", "NONE", "NULL", "NAN", "NOT_AVAILABLE"}
LOGGER = logging.getLogger(__name__)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _csv(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


def _time(value: Any) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(text)
        return result.replace(tzinfo=result.tzinfo or timezone.utc)
    except ValueError:
        return None


def _unknown(value: Any) -> bool:
    return value is None or str(value).strip().upper() in UNKNOWN_VALUES


def _field_unknown(field: str, value: Any) -> bool:
    if field == "primary_blocker" and str(value or "").strip().upper() == "NONE":
        return False
    return _unknown(value)


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _canonical_trade(row: Mapping[str, Any]) -> dict[str, Any]:
    entry = row.get("entry", row.get("entry_price"))
    exit_price = row.get("exit_price", row.get("exit"))
    stop = row.get("sl", row.get("stop_loss"))
    target = row.get("tp", row.get("take_profit"))
    entry_n, stop_n, target_n = _number(entry), _number(stop), _number(target)
    rr = row.get("risk_reward", row.get("rr", row.get("RR")))
    if _unknown(rr) and None not in (entry_n, stop_n, target_n) and entry_n != stop_n:
        rr = round(abs(target_n - entry_n) / abs(entry_n - stop_n), 6)
    return {
        **dict(row),
        "entry_price": entry,
        "exit_price": exit_price,
        "timestamp": row.get("timestamp", row.get("opened_at")),
        "risk_reward": rr,
    }


def validate_research_trade(row: Mapping[str, Any]) -> list[str]:
    """Return every absent/unknown mandatory research field without raising."""
    canonical = _canonical_trade(row)
    missing = [field for field in RESEARCH_FIELDS if _field_unknown(field, canonical.get(field))]
    if missing:
        LOGGER.warning("research trade %s missing fields: %s", canonical.get("trade_id", "UNKNOWN"), ", ".join(missing))
    return missing


def coverage_report(rows: Sequence[Mapping[str, Any]],
                    fields: Sequence[str] = RESEARCH_FIELDS) -> dict[str, Any]:
    total = len(rows); features = []
    for field in fields:
        missing = sum(field not in row for row in rows)
        null = sum(field in row and row.get(field) is None for row in rows)
        unknown = sum(field in row and row.get(field) is not None and _field_unknown(field, row.get(field)) for row in rows)
        covered = total - missing - null - unknown
        pct = lambda count: round(count / total * 100, 2) if total else 0.0
        features.append({"feature": field, "coverage_pct": pct(covered), "missing_pct": pct(missing),
                         "unknown_pct": pct(unknown), "null_pct": pct(null), "total_trades": total,
                         "covered": covered, "missing": missing, "unknown": unknown, "null": null})
    average = round(sum(row["coverage_pct"] for row in features) / len(features), 2) if features else 0.0
    missing_total = sum(row["missing"] for row in features)
    unknown_total = sum(row["unknown"] for row in features)
    warnings = []
    if any(row["coverage_pct"] < 90 for row in features): warnings.append("COVERAGE_BELOW_90")
    if total and any(row["unknown_pct"] > 5 for row in features): warnings.append("UNKNOWN_ABOVE_5")
    if missing_total: warnings.append("MISSING_ABOVE_0")
    status = "GOOD" if not warnings and average >= 98 else ("WARNING" if average >= 90 else "POOR")
    return {"coverage_pct": average, "features": features, "missing": missing_total,
            "unknown": unknown_total, "warnings": warnings, "status": status}


class ResearchDataQuality:
    """Recover research features from existing immutable runtime artifacts."""

    def __init__(self, *, base_dir: str | Path = BASE_DIR,
                 registry: TradeRegistry | None = None,
                 sources: Mapping[str, Sequence[Mapping[str, Any]]] | None = None) -> None:
        self.base_dir = Path(base_dir)
        self.registry = registry or TradeRegistry(self.base_dir / "trades.csv")
        if sources is None:
            sources = {
                "trade_registry": self.registry.get_complete_trades(),
                "decision_snapshot": self._snapshots(),
                "trade_market_context": _csv(self.base_dir / "trade_market_context.csv"),
                "setup_history": _csv(self.base_dir / "setup_history_v3.csv"),
                "decision_log": _csv(self.base_dir / "decision_debug.csv"),
                "signals": _csv(self.base_dir / "signals_v3.csv"),
            }
        self.sources = {name: [dict(row) for row in rows] for name, rows in sources.items()}

    def _snapshots(self) -> list[dict[str, Any]]:
        payload = _json(self.base_dir / "decision_snapshot.json")
        history = payload.get("history", [])
        rows = [dict(row) for row in history if isinstance(row, dict)]
        if isinstance(payload.get("latest"), dict): rows.append(dict(payload["latest"]))
        return rows

    @staticmethod
    def _matches(trade: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
        trade_id, row_id = str(trade.get("trade_id", "")), str(row.get("trade_id", ""))
        if trade_id and row_id and trade_id == row_id: return True
        return (str(trade.get("symbol", "")).upper() == str(row.get("symbol", "")).upper()
                and (not row.get("direction") or str(trade.get("direction", "")).upper() == str(row.get("direction", "")).upper()))

    def _nearest(self, trade: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        opened = _time(trade.get("opened_at", trade.get("timestamp")))
        candidates = []
        for row in rows:
            if not self._matches(trade, row): continue
            timestamp = _time(row.get("timestamp", row.get("opened_at")))
            if opened is None or timestamp is None or timestamp <= opened:
                candidates.append((timestamp or datetime.min.replace(tzinfo=timezone.utc), row))
        return dict(max(candidates, key=lambda item: item[0])[1]) if candidates else {}

    @staticmethod
    def _value(field: str, row: Mapping[str, Any]) -> Any:
        aliases = {
            "timestamp": ("timestamp", "opened_at"), "entry_price": ("entry_price", "entry"),
            "exit_price": ("exit_price", "exit"), "timeframe": ("timeframe", "interval"),
            "trend_alignment": ("trend_alignment",), "market_regime": ("market_regime", "regime"),
            "risk_reward": ("risk_reward", "rr", "RR"),
        }
        for key in aliases.get(field, (field,)):
            if not _field_unknown(field, row.get(key)): return row.get(key)
        return None

    def recover_trade(self, trade: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        result = _canonical_trade(trade); recovered = {}
        source_order = ("trade_registry", "decision_snapshot", "trade_market_context", "setup_history", "decision_log", "signals")
        contexts = {name: self._nearest(result, self.sources.get(name, [])) for name in source_order}
        for field in RESEARCH_FIELDS:
            if not _field_unknown(field, result.get(field)): continue
            for source in source_order:
                value = self._value(field, contexts[source])
                if not _field_unknown(field, value):
                    result[field] = value; recovered[field] = source; break
        if _unknown(result.get("trend_alignment")):
            trend = str(contexts["trade_market_context"].get("trend", "")).upper()
            direction = str(result.get("direction", "")).upper()
            if trend in {"LONG", "SHORT"} and direction in {"LONG", "SHORT"}:
                result["trend_alignment"] = "ALIGNED" if trend == direction else "COUNTER_TREND"
                recovered["trend_alignment"] = "trade_market_context:derived"
        if _field_unknown("timeframe", result.get("timeframe")):
            result["timeframe"] = "1h"; recovered["timeframe"] = "runtime_default"
        if _field_unknown("primary_blocker", result.get("primary_blocker")):
            result["primary_blocker"] = "NONE"; recovered["primary_blocker"] = "no_recorded_blocker"
        for field in RESEARCH_FIELDS:
            if field not in result or result[field] is None:
                result[field] = "UNKNOWN"
        return result, recovered

    def backfill(self, *, write: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        source = self.registry.get_complete_trades(); enriched = []; recovered_counts = Counter(); unable = Counter()
        details = []
        for trade in source:
            row, recovered = self.recover_trade(trade); missing = validate_research_trade(row)
            enriched.append(row); recovered_counts.update(recovered.keys()); unable.update(field for field in BACKFILL_FIELDS if field in missing)
            details.append({"trade_id": row.get("trade_id"), "recovered": recovered, "unable": missing})
        coverage = coverage_report(enriched)
        report = {"generated_at": datetime.now(timezone.utc).isoformat(), "mode": "RESEARCH_DERIVED_BACKFILL",
                  "source_trades_unchanged": True, "complete_trades": len(enriched),
                  "recovered": dict(sorted(recovered_counts.items())), "recovered_total": sum(recovered_counts.values()),
                  "unable": dict(sorted(unable.items())), "unable_total": sum(unable.values()),
                  "coverage": coverage, "details": details,
                  "restrictions": ["NO_LIVE_MUTATION", "NO_TRADING_LOGIC_CHANGE", "NO_DECISION_ENGINE_CHANGE"]}
        if write:
            _atomic_json(self.base_dir / "reports/research_trades_enriched.json", {"schema_version": 1, "trades": enriched})
            _atomic_json(self.base_dir / "reports/backfill_report.json", report)
        return enriched, report

    def build_report(self, *, write: bool = True) -> dict[str, Any]:
        rows, backfill = self.backfill(write=write); coverage = coverage_report(rows)
        issues = [{"trade_id": row.get("trade_id"), "missing_fields": [field for field in RESEARCH_FIELDS if _field_unknown(field, row.get(field))]} for row in rows]
        issues = [row for row in issues if row["missing_fields"]]
        report = {"schema_version": 2, "generated_at": datetime.now(timezone.utc).isoformat(),
                  "mode": "RESEARCH_DATA_QUALITY", "status": coverage["status"],
                  "complete_trades": len(rows), "coverage": coverage,
                  "recovered": backfill["recovered_total"], "unknown": coverage["unknown"],
                  "missing": coverage["missing"], "validation_warnings": issues,
                  "threshold_checks": coverage["warnings"],
                  "restrictions": ["LIVE_UNCHANGED", "TRADING_LOGIC_UNCHANGED", "DECISION_ENGINES_UNCHANGED"]}
        if write: _atomic_json(self.base_dir / "reports/data_quality.json", report)
        return report


def build_decision_snapshot(*, base_dir: str | Path = BASE_DIR,
                            symbol: str | None = None,
                            opened_at: Any = None,
                            extra: Mapping[str, Any] | None = None,
                            write: bool = True) -> dict[str, Any]:
    """Build a best-effort snapshot from already persisted decision artifacts."""
    root = Path(base_dir); rows = _csv(root / "decision_debug.csv")
    if symbol: rows = [row for row in rows if str(row.get("symbol", "")).upper() == symbol.upper()]
    cutoff = _time(opened_at)
    eligible = [row for row in rows if cutoff is None or (_time(row.get("timestamp")) and _time(row.get("timestamp")) <= cutoff)]
    row = dict(eligible[-1] if eligible else (rows[-1] if rows else {})); extra = dict(extra or {})
    context_rows = _csv(root / "trade_market_context.csv")
    context = next((item for item in reversed(context_rows) if not symbol or str(item.get("symbol", "")).upper() == symbol.upper()), {})
    snapshot = {
        "timestamp": extra.get("timestamp", opened_at or row.get("timestamp") or datetime.now(timezone.utc).isoformat()),
        "symbol": symbol or extra.get("symbol") or row.get("symbol"), "direction": extra.get("direction", row.get("direction")),
        "trend": {"long": row.get("trend_long"), "short": row.get("trend_short"), "reason": row.get("trend_reason")},
        "structure": {"long": row.get("structure_long"), "short": row.get("structure_short"), "reason": row.get("structure_reason")},
        "momentum": {"long": row.get("momentum_long"), "short": row.get("momentum_short"), "reason": row.get("momentum_reason")},
        "risk": {"long": row.get("risk_long"), "short": row.get("risk_short"), "reason": row.get("risk_reason")},
        "raw_score": _number(row.get("score")), "final_score": _number(extra.get("score", row.get("score"))),
        "confidence": _number(extra.get("confidence", row.get("confidence"))), "quality": extra.get("quality", row.get("quality")),
        "primary_blocker": extra.get("primary_blocker", context.get("primary_blocker")),
        "market_regime": extra.get("market_regime", context.get("market_regime")),
        "volatility": extra.get("volatility", context.get("volatility")),
        "timeframe": extra.get("timeframe", context.get("timeframe", "1h")),
        "reason": extra.get("reason", row.get("summary")), "mode": "DIAGNOSTIC_ONLY",
    }
    if write:
        path = root / "decision_snapshot.json"; existing = _json(path); history = existing.get("history", [])
        if not isinstance(history, list): history = []
        history.append(snapshot); _atomic_json(path, {"schema_version": 1, "latest": snapshot, "history": history[-500:]})
    return snapshot


def load_enriched_trades(base_dir: str | Path = BASE_DIR) -> list[dict[str, Any]]:
    payload = _json(Path(base_dir) / "reports/research_trades_enriched.json")
    return [dict(row) for row in payload.get("trades", []) if isinstance(row, dict)]


def run_backfill_pipeline(*, base_dir: str | Path = BASE_DIR) -> dict[str, Any]:
    """Backfill derived history and rebuild dependent research reports."""
    root = Path(base_dir); engine = ResearchDataQuality(base_dir=root)
    _, backfill = engine.backfill(write=True)
    from signal_quality_analyzer import SignalQualityAnalyzer
    from loss_attribution import LossAttribution
    from research_dashboard import build_report as build_dashboard, save_report as save_dashboard
    SignalQualityAnalyzer(base_dir=root,
        report_path=root/"reports/signal_quality.json", summary_path=root/"reports/signal_quality_summary.txt",
        history_dir=root/"reports/signal_quality_history").write_reports()
    LossAttribution(base_dir=root,
        report_path=root/"reports/loss_attribution.json", summary_path=root/"reports/loss_attribution_summary.txt",
        history_dir=root/"reports/loss_attribution_history").write_reports()
    save_dashboard(build_dashboard(base_dir=root), dashboard_json=root/"reports/research_dashboard.json",
                   summary_path=root/"reports/research_dashboard_summary.txt",
                   history_dir=root/"reports/research_dashboard_history")
    engine.build_report(write=True)
    return backfill


def format_data_quality(report: Mapping[str, Any], view: str = "") -> str:
    coverage = report.get("coverage", {}); features = coverage.get("features", [])
    if view == "coverage":
        lines = ["📊 Data Coverage"]
        lines.extend(f"{str(row.get('feature')).replace('_',' ').title()}: {row.get('coverage_pct',0):.1f}%" for row in features)
        return "\n".join(lines)
    return "\n".join(["📋 Research Data Quality", f"Coverage: {coverage.get('coverage_pct',0):.1f}%",
                      f"Recovered: {report.get('recovered',0)}", f"Unknown: {report.get('unknown',0)}",
                      f"Missing: {report.get('missing',0)}", f"Status: {report.get('status','NO_DATA')}"])


def main() -> None:
    report = ResearchDataQuality().build_report(write=True)
    print(format_data_quality(report))


if __name__ == "__main__":
    main()
