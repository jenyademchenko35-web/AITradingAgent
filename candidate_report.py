"""Candidate Laboratory aggregate and baseline comparison reports."""

from __future__ import annotations

import csv
import json
import math
import os
import tempfile
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from candidate_laboratory import CONFIG_FILE, DECISIONS_FILE, TRADES_FILE

BASE_DIR = Path(__file__).resolve().parent
REPORT_FILE = BASE_DIR / "reports" / "candidate_laboratory.json"
COMPARISON_FILE = BASE_DIR / "reports" / "candidate_comparison.json"
LOCK = threading.RLock()
MIN_COMPLETE = 30


def _num(value: Any) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or not path.stat().st_size:
        return []
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            return [row for row in csv.DictReader(stream) if row and any(row.values())]
    except (OSError, csv.Error):
        return []


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def calculate_metrics(trades: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(trades)
    complete = [row for row in rows if row.get("status") in ("WIN", "LOSS")]
    returns = [_num(row.get("pnl_r")) for row in complete]
    gains = sum(value for value in returns if value > 0)
    losses = abs(sum(value for value in returns if value < 0))
    equity = peak = max_dd = 0.0
    for value in returns:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    wins = sum(value > 0 for value in returns)
    return {
        "complete_trades": len(complete), "wins": wins,
        "losses": len(complete) - wins,
        "winrate": round(wins / len(complete) * 100, 4) if complete else 0.0,
        "profit_factor": round(gains / losses, 4) if losses else (None if not gains else "INF"),
        "net_r": round(sum(returns), 4), "max_drawdown_r": round(max_dd, 4),
        "average_r": round(sum(returns) / len(returns), 4) if returns else 0.0,
        "average_rr": round(sum(_num(row.get("rr")) for row in complete) / len(complete), 4) if complete else 0.0,
    }


def _breakdown(trades: list[dict[str, str]], field: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for trade in trades:
        groups[trade.get(field) or "UNKNOWN"].append(trade)
    return {key: calculate_metrics(value) for key, value in sorted(groups.items())}


def build_reports(
    *, decisions_path: str | Path = DECISIONS_FILE,
    trades_path: str | Path = TRADES_FILE,
    config_path: str | Path = CONFIG_FILE,
    report_path: str | Path = REPORT_FILE,
    comparison_path: str | Path = COMPARISON_FILE,
) -> tuple[dict[str, Any], dict[str, Any]]:
    decisions_path, trades_path = Path(decisions_path), Path(trades_path)
    try:
        configs = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        configs = {}
    candidate_ids = [
        key for key, value in configs.items()
        if key != "session_utc" and isinstance(value, dict) and value.get("enabled")
    ]
    decisions, trades = _rows(decisions_path), _rows(trades_path)
    generated = datetime.now(timezone.utc).isoformat()
    source_mtime = max(
        [path.stat().st_mtime for path in (decisions_path, trades_path) if path.exists()],
        default=0,
    )
    candidates = {}
    for candidate_id in candidate_ids:
        cdec = [row for row in decisions if row.get("candidate_id") == candidate_id]
        ctrades = [row for row in trades if row.get("candidate_id") == candidate_id]
        metrics = calculate_metrics(ctrades)
        complete = metrics["complete_trades"]
        status = "READY_FOR_COMPARISON" if complete >= MIN_COMPLETE else (
            "COLLECTING" if cdec or ctrades else "INSUFFICIENT_DATA"
        )
        candidates[candidate_id] = {
            "total_decisions": len(cdec),
            "setups": sum(row.get("decision") in ("SETUP", "HIGH PRIORITY") for row in cdec),
            "shadow_trades": len(ctrades),
            "open_trades": sum(row.get("status") == "OPEN" for row in ctrades),
            "closed_trades": sum(row.get("status") != "OPEN" for row in ctrades),
            **metrics,
            "by_symbol": _breakdown(ctrades, "symbol"),
            "by_direction": _breakdown(ctrades, "direction"),
            "by_quality": _breakdown(ctrades, "quality"),
            "by_market_regime": _breakdown(ctrades, "market_regime"),
            "by_session": _breakdown(ctrades, "session"),
            "generated_at": generated,
            "source_modified_at": datetime.fromtimestamp(source_mtime, timezone.utc).isoformat() if source_mtime else None,
            "status": status,
        }
    overall = (
        "READY_FOR_COMPARISON"
        if candidate_ids and all(candidates[cid]["complete_trades"] >= MIN_COMPLETE for cid in candidate_ids)
        else ("COLLECTING" if decisions or trades else "INSUFFICIENT_DATA")
    )
    if overall != "READY_FOR_COMPARISON":
        for item in candidates.values():
            if item["status"] == "READY_FOR_COMPARISON":
                item["status"] = "COLLECTING"
    report = {"generated_at": generated, "status": overall, "minimum_complete_trades": MIN_COMPLETE, "candidates": candidates}
    baseline = candidates.get("LIVE_BASELINE", {})
    comparisons = {}
    for candidate_id, item in candidates.items():
        if candidate_id == "LIVE_BASELINE":
            continue
        sample = min(int(item.get("complete_trades", 0)), int(baseline.get("complete_trades", 0)))
        confidence = (
            "STRONG_EVIDENCE" if sample >= 100 else
            "MODERATE_EVIDENCE" if sample >= 60 else
            "WEAK_EVIDENCE" if sample >= 30 else "INSUFFICIENT_DATA"
        )
        def finite_pf(value: Any) -> float:
            return _num(value) if value != "INF" else 0.0
        comparisons[candidate_id] = {
            "baseline": "LIVE_BASELINE",
            "delta_pf": round(finite_pf(item.get("profit_factor")) - finite_pf(baseline.get("profit_factor")), 4),
            "delta_net_r": round(_num(item.get("net_r")) - _num(baseline.get("net_r")), 4),
            "delta_winrate": round(_num(item.get("winrate")) - _num(baseline.get("winrate")), 4),
            "delta_max_drawdown": round(_num(item.get("max_drawdown_r")) - _num(baseline.get("max_drawdown_r")), 4),
            "delta_trade_count": int(item.get("complete_trades", 0)) - int(baseline.get("complete_trades", 0)),
            "sample_size": sample, "confidence_status": confidence,
            "interpretation": "Research comparison only; no live promotion conclusion.",
        }
    comparison = {"generated_at": generated, "status": overall, "comparisons": comparisons}
    with LOCK:
        _atomic_json(Path(report_path), report)
        _atomic_json(Path(comparison_path), comparison)
    return report, comparison


if __name__ == "__main__":
    build_reports()
