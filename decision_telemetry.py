"""Append-only DecisionEngine telemetry; it never participates in decisions."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

FIELDS = ("timestamp", "cycle_id", "symbol", "direction", "trend_score", "structure_score",
          "momentum_score", "risk_score", "volume_score", "market_regime", "raw_signal",
          "final_decision", "final_filter_status", "execution_status", "blocking_reasons")


def telemetry_row(*, timestamp: str, cycle_id: str, symbol: str, decision: Any,
                  trend: Any, structure: Any, momentum: Any, risk: Any, market: Any) -> dict[str, str]:
    """Serialize already-computed values without mutating any agent object."""
    tf = market.tf1h
    component = lambda value: max(float(value.long), float(value.short))
    blockers = list(getattr(decision, "veto_reasons", []) or []) + list(getattr(decision, "failed_filters", []) or [])
    return {
        "timestamp": timestamp, "cycle_id": cycle_id, "symbol": symbol,
        "direction": str(getattr(decision, "direction", "")),
        "trend_score": str(component(trend)), "structure_score": str(component(structure)),
        "momentum_score": str(component(momentum)), "risk_score": str(component(risk)),
        "volume_score": str(getattr(tf, "volume_ratio", "")),
        "market_regime": str(getattr(tf, "trend_ema", "UNKNOWN")),
        "raw_signal": str(getattr(decision, "raw_signal_status", getattr(decision, "signal", ""))),
        "final_decision": str(getattr(decision, "signal", "")),
        "final_filter_status": str(getattr(decision, "final_filter_status", "")),
        "execution_status": str(getattr(decision, "execution_status", "")),
        "blocking_reasons": json.dumps(blockers, ensure_ascii=False),
    }


def append_telemetry(row: dict[str, str], path: Path = Path("decision_engine_telemetry.csv")) -> None:
    """Best-effort append; diagnostics must never break a trading cycle."""
    try:
        needs_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerow({field: row.get(field, "") for field in FIELDS})
    except OSError:
        pass
