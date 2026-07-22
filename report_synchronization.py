"""Freshness and consistency coordinator for Telegram research reports.

This module is deliberately read-only with respect to ``trades.csv``.  It
refreshes derived artifacts from that file and makes their provenance visible.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from decision_intelligence import run as run_decision_intelligence
from promotion_gate import run as run_promotion_gate
from research_dashboard import build_report as build_dashboard, save_report as save_dashboard
from trade_metrics_normalizer import aggregate_trade_metrics, read_trade_rows
from trade_registry import TradeRegistry, save_reports as save_registry_reports


BASE_DIR = Path(__file__).resolve().parent
TRADES_PATH = Path("trades.csv")
REPORTS = {
    "research_dashboard": Path("reports/research_dashboard.json"),
    "promotion_gate": Path("reports/promotion_gate.json"),
    "decision_intelligence": Path("decision_learning.json"),
}
AUDIT_PATH = Path("reports/data_source_audit.json")
CONSISTENCY_PATH = Path("reports/consistency_report.json")
_SYNC_LOCK = threading.RLock()

DATA_SOURCE_AUDIT = {
    "stats": {"source": "trades.csv", "sample": "all closed trades; performance metrics use COMPLETE rows"},
    "dashboard": {"source": "reports/research_dashboard.json", "canonical_source": "trades.csv", "sample": "all closed trades; performance metrics use COMPLETE rows"},
    "ready": {"source": "reports/promotion_gate.json", "canonical_source": "trades.csv", "sample": "COMPLETE trades for sample size; replay metrics for promotion evidence"},
    "learning": {"source": "decision_learning.json", "upstream": "reports/decision_engine_v2.json", "sample": "COMPLETE decisions available in DecisionEngine v2 comparisons"},
    "modules": {"source": "decision_learning.json", "upstream": "reports/decision_engine_v2.json", "sample": "COMPLETE decisions available in DecisionEngine v2 comparisons"},
    "accuracy": {"source": "decision_learning.json", "upstream": "reports/decision_engine_v2.json", "sample": "COMPLETE decisions available in DecisionEngine v2 comparisons"},
    "rootcause": {"source": "decision_learning.json", "upstream": "reports/decision_engine_v2.json", "sample": "losing COMPLETE decisions available in DecisionEngine v2 comparisons"},
}


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _iso_mtime(path: Path) -> str:
    if not path.exists():
        return ""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def report_is_current(report_path: Path, trades_path: Path) -> bool:
    """Require an explicit source timestamp equal to the current source mtime."""
    report = _read(report_path)
    return bool(report and report.get("generated_at") and report.get("source_trades_modified") == _iso_mtime(trades_path))


def write_data_source_audit(*, base_dir: Path = BASE_DIR) -> dict[str, Any]:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_trades_modified": _iso_mtime(base_dir / TRADES_PATH),
        "commands": DATA_SOURCE_AUDIT,
    }
    _atomic_json(base_dir / AUDIT_PATH, payload)
    return payload


def build_consistency_report(*, base_dir: Path = BASE_DIR) -> dict[str, Any]:
    """Compare Dashboard baseline with the same canonical metrics as /stats."""
    dashboard = _read(base_dir / REPORTS["research_dashboard"])
    baseline = dashboard.get("baseline_metrics", {})
    stats = aggregate_trade_metrics(read_trade_rows(base_dir / TRADES_PATH))
    fields = {
        "Closed Trades": ("closed_trades", "closed_trades"),
        "Winrate": ("winrate", "winrate"),
        "Profit Factor": ("profit_factor", "profit_factor"),
        "Net R": ("net_r", "net_r"),
        "Max Drawdown": ("max_drawdown_r", "max_drawdown_r"),
    }
    mismatches = []
    comparisons = {}
    for label, (dashboard_key, stats_key) in fields.items():
        left = baseline.get(dashboard_key, 0)
        right = stats.get(stats_key, 0)
        comparisons[label] = {"dashboard": left, "stats": right}
        try:
            equal = abs(float(left) - float(right)) <= 1e-6
        except (TypeError, ValueError):
            equal = left == right
        if not equal:
            mismatches.append({"metric": label, "dashboard": left, "stats": right})
    payload = {
        "status": "FAILED" if mismatches else "PASSED",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_trades_modified": _iso_mtime(base_dir / TRADES_PATH),
        "comparisons": comparisons,
        "mismatches": mismatches,
    }
    if len(mismatches) == 1:
        payload.update(mismatches[0])
    _atomic_json(base_dir / CONSISTENCY_PATH, payload)
    return payload


def _synchronize_reports(*, base_dir: Path = BASE_DIR, force: bool = False) -> dict[str, Any]:
    """Refresh the report suite in its declared order and verify consistency."""
    root = Path(base_dir)
    trades = root / TRADES_PATH
    current = {name: report_is_current(root / path, trades) for name, path in REPORTS.items()}
    refresh = force or not all(current.values())
    refreshed: list[str] = []
    if refresh:
        registry = TradeRegistry(trades)
        registry_report, quality_report = registry.build_reports()
        source_mtime = _iso_mtime(trades)
        registry_report["source_trades_modified"] = source_mtime
        quality_report["source_trades_modified"] = source_mtime
        save_registry_reports(
            registry_report,
            quality_report,
            registry_path=root / "reports/trade_registry.json",
            quality_path=root / "reports/data_quality.json",
            summary_path=root / "reports/data_quality_summary.txt",
            history_dir=root / "reports/data_quality_history",
        )
        refreshed.append("trades")

        dashboard = build_dashboard(base_dir=root)
        dashboard["source_trades_modified"] = source_mtime
        save_dashboard(
            dashboard,
            dashboard_json=root / REPORTS["research_dashboard"],
            summary_path=root / "reports/research_dashboard_summary.txt",
            history_dir=root / "reports/research_dashboard_history",
        )
        refreshed.append("research_dashboard")

        run_promotion_gate(base_dir=root)
        refreshed.append("promotion_gate")
        run_decision_intelligence(base_dir=root)
        refreshed.append("decision_intelligence")

    audit = write_data_source_audit(base_dir=root)
    consistency = build_consistency_report(base_dir=root)
    return {"refreshed": bool(refreshed), "reports": refreshed, "audit": audit, "consistency": consistency}


def synchronize_reports(*, base_dir: Path = BASE_DIR, force: bool = False) -> dict[str, Any]:
    """Serialize refreshes so concurrent Telegram commands share one cycle."""
    with _SYNC_LOCK:
        return _synchronize_reports(base_dir=base_dir, force=force)


def datasource_status(*, base_dir: Path = BASE_DIR) -> dict[str, dict[str, Any]]:
    root = Path(base_dir)
    trades = root / TRADES_PATH
    rows: dict[str, dict[str, Any]] = {}
    for name, relative in REPORTS.items():
        payload = _read(root / relative)
        audit_key = "dashboard" if name == "research_dashboard" else "ready" if name == "promotion_gate" else "learning"
        rows[name] = {
            "source": str(relative),
            "generated_at": payload.get("generated_at", "N/A"),
            "source_trades_modified": payload.get("source_trades_modified", "N/A"),
            "status": "UP TO DATE" if report_is_current(root / relative, trades) else "OUTDATED",
            "sample": DATA_SOURCE_AUDIT[audit_key]["sample"],
        }
    return rows
