"""Read-only Shadow Research Lab comparison report.

Usage: python strategy_diagnostics.py --input research_lab_shadow_history.csv \
    --telemetry decision_engine_telemetry.csv --output strategy_diagnostics.json
"""
from __future__ import annotations

import argparse, csv, json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from research_lab_v2.analytics import calculate_metrics

TARGETS = ("MOMENTUM_RELAXED", "TREND_PULLBACK", "CONSERVATIVE")

def _float(value: Any) -> float | None:
    try: return float(value)
    except (TypeError, ValueError): return None

def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists(): return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))

def _feature_importance(rows: list[dict[str, str]]) -> dict[str, float]:
    """Evidence-only win/loss mean deltas from persisted feature snapshots."""
    values: dict[str, list[tuple[float, bool]]] = {}
    for row in rows:
        pnl = _float(row.get("pnl_r"))
        try: snapshot = json.loads(row.get("feature_snapshot", "{}"))
        except (TypeError, ValueError): snapshot = {}
        if pnl is None or not isinstance(snapshot, dict): continue
        for key, value in snapshot.items():
            number = _float(value)
            if number is not None: values.setdefault(key, []).append((number, pnl > 0))
    result = {}
    for key, pairs in values.items():
        wins, losses = [v for v, good in pairs if good], [v for v, good in pairs if not good]
        if wins and losses: result[key] = sum(wins) / len(wins) - sum(losses) / len(losses)
    return dict(sorted(result.items(), key=lambda item: abs(item[1]), reverse=True)[:10])

def build_report(history: list[dict[str, str]], telemetry: list[dict[str, str]]) -> dict[str, Any]:
    strategies: dict[str, Any] = {}
    for strategy in TARGETS:
        rows = [r for r in history if str(r.get("strategy_id", "")).upper() == strategy]
        closed = [r for r in rows if str(r.get("status", "")).upper() in {"CLOSED", "TAKE_PROFIT", "STOP_LOSS", "INVALIDATED", "TIMEOUT"} or r.get("pnl_r") not in (None, "")]
        values = [value for r in closed if (value := _float(r.get("pnl_r"))) is not None]
        holding = [value for r in closed if (value := _float(r.get("holding_candles"))) is not None]
        reasons = Counter(str(r.get("exit_reason") or r.get("blocked_reason") or "UNKNOWN") for r in rows)
        metrics = calculate_metrics(values)
        strategies[strategy] = {
            "signals": len(rows), "opened_shadow_trades": len(rows), "closed_trades": len(values),
            "metrics": metrics, "average_holding_candles": sum(holding)/len(holding) if holding else None,
            "blocking_reasons": dict(reasons), "feature_importance": _feature_importance(closed),
            "recommendation": "INSUFFICIENT_CLOSED_SHADOW_TRADES" if len(values) < 100 else "REQUIRES_WALK_FORWARD_AND_LIVE_BASELINE_COMPARISON",
        }
    blockers = Counter()
    for row in telemetry:
        try: blockers.update(json.loads(row.get("blocking_reasons", "[]")))
        except (ValueError, TypeError): pass
    return {"generated_at": datetime.now().astimezone().isoformat(), "source": "RESEARCH_LAB_SHADOW_ONLY",
            "telemetry_rows": len(telemetry), "decision_blocking_reasons": dict(blockers),
            "strategies": strategies,
            "note": "This report is observational. It never promotes, changes, or executes a strategy."}

def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a read-only Shadow Research Lab diagnostics report.")
    parser.add_argument("--input", type=Path, default=Path("research_lab_shadow_history.csv"))
    parser.add_argument("--telemetry", type=Path, default=Path("decision_engine_telemetry.csv"))
    parser.add_argument("--output", type=Path, default=Path("strategy_diagnostics.json"))
    args = parser.parse_args()
    report = build_report(_read_csv(args.input), _read_csv(args.telemetry))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "strategies": list(report["strategies"])}, ensure_ascii=False))
    return 0

if __name__ == "__main__": raise SystemExit(main())
