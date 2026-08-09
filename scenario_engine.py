"""Scenario Engine v1: deterministic, fail-open observer of published market data."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

BASE_DIR = Path(__file__).resolve().parent
REPORT = BASE_DIR / "scenario_report.json"
HISTORY = BASE_DIR / "scenario_history.jsonl"
CHANGES = BASE_DIR / "scenario_changes.json"
LOG = BASE_DIR / "logs" / "scenario_engine.log"


def _enabled() -> bool: return os.getenv("SCENARIO_ENGINE_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
def _history_enabled() -> bool: return os.getenv("SCENARIO_HISTORY_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
def _threshold() -> int:
    try: return max(1, min(100, int(os.getenv("SCENARIO_CHANGE_THRESHOLD", "10"))))
    except ValueError: return 10


def _number(value: Any) -> float | None:
    try: return float(value) if value not in (None, "") else None
    except (TypeError, ValueError): return None


def _now() -> str: return datetime.now(timezone.utc).isoformat()


def _log(event: str, **fields: Any) -> None:
    """Best-effort diagnostics: logging can never interrupt the agent cycle."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle: handle.write(json.dumps({"timestamp": _now(), "event": event, **fields}, ensure_ascii=False) + "\n")
    except OSError: pass


def _text(value: Any) -> str | None:
    return str(value).strip() if value not in (None, "") else None


def _probabilities(context: Mapping[str, Any], impulse: Mapping[str, Any]) -> tuple[dict[str, int], list[str], list[str]] | None:
    """Produce explainable probabilities from published values only; no future data."""
    trend = _number(context.get("trend_score")); score = _number(context.get("score")); confidence = _number(context.get("confidence")); impulse_probability = _number(impulse.get("impulse_probability", context.get("impulse_probability")))
    if trend is None and score is None and confidence is None and impulse_probability is None: return None
    side = (_text(context.get("side") or context.get("direction")) or "").upper()
    regime = (_text(context.get("market_regime")) or "UNKNOWN").upper()
    adx = _number(context.get("adx")); volume = _number(context.get("volume_ratio")); failed = [str(item) for item in context.get("failed_filters", []) or []]
    bullish, bearish, neutral = 33.0, 33.0, 34.0
    reasons, blockers = [], list(failed)
    if side == "LONG": bullish += 14; bearish -= 7; reasons.append("Published directional context is LONG")
    elif side == "SHORT": bearish += 14; bullish -= 7; reasons.append("Published directional context is SHORT")
    if trend is not None:
        if trend >= 50: bullish += 10; bearish -= 4; reasons.append("Published trend score supports continuation")
        elif trend <= 20: bearish += 6; neutral += 4; reasons.append("Published trend score is weak")
    if score is not None:
        score_bonus = min(10, max(0, score) * .25)
        if side == "LONG": bullish += score_bonus
        elif side == "SHORT": bearish += score_bonus
        else: neutral += score_bonus
    if confidence is not None:
        if confidence >= 70: reasons.append("Published confidence is elevated")
        elif confidence < 40: neutral += 8; blockers.append("Published confidence is low")
    if impulse_probability is not None:
        if side == "LONG": bullish += (impulse_probability - 50) * .16
        elif side == "SHORT": bearish += (impulse_probability - 50) * .16
        else: neutral += max(0, 50 - impulse_probability) * .1
        reasons.append("Impulse Probability used as observer input")
    if adx is not None and adx >= 25:
        if side == "LONG": bullish += 6
        elif side == "SHORT": bearish += 6
        reasons.append("ADX > 25")
    if volume is not None and volume >= 1:
        if side == "LONG": bullish += 5
        elif side == "SHORT": bearish += 5
        reasons.append("Volume above published average")
    if regime in {"RANGE", "LOW_VOLATILITY"}: neutral += 16; blockers.append("Range or low-volatility regime")
    if any("risk" in item.lower() for item in failed): neutral += 10; blockers.append("Published risk warning")
    raw = {"BULLISH_CONTINUATION": max(0.0, bullish), "BEARISH_REVERSAL": max(0.0, bearish), "RANGE": max(0.0, neutral)}
    total = sum(raw.values()) or 1.0
    rounded = {name: int(round(value * 100 / total)) for name, value in raw.items()}
    rounded[max(rounded, key=rounded.get)] += 100 - sum(rounded.values())
    return rounded, reasons, blockers


def evaluate(context: Mapping[str, Any], impulse: Mapping[str, Any] | None = None) -> dict[str, Any]:
    impulse = impulse or {}
    generated_at = _text(context.get("timestamp")) or _now()
    probabilities_data = _probabilities(context, impulse)
    status = (_text(context.get("status") or context.get("signal")) or "NO TRADE").upper()
    support, resistance = context.get("support"), context.get("resistance")
    if probabilities_data is None:
        return {"symbol": _text(context.get("symbol")) or "UNKNOWN", "generated_at": generated_at, "cycle_id": context.get("cycle_id"), "status": "INSUFFICIENT_DATA", "primary_scenario": None, "primary_probability": None, "confidence": None, "alternative_scenarios": [], "market_regime": _text(context.get("market_regime")) or "UNKNOWN", "trend_context": None, "momentum_context": None, "structure_context": None, "volatility_context": None, "key_support": support, "key_resistance": resistance, "invalidation_conditions": [], "confirmation_conditions": [], "reasons": [], "blockers": ["Published scenario inputs are insufficient"], "uncertainty_factors": ["No score, trend, confidence or impulse probability published"], "data_quality": "INSUFFICIENT_DATA", "source_freshness": generated_at}
    probabilities, reasons, blockers = probabilities_data
    primary = max(probabilities, key=probabilities.get)
    confidence = "HIGH" if probabilities[primary] >= 70 else "MEDIUM" if probabilities[primary] >= 50 else "LOW"
    alternatives = [{"scenario": name, "probability": probability} for name, probability in probabilities.items() if name != primary]
    confirmations = ["Price confirms published resistance" if primary == "BULLISH_CONTINUATION" else "Price confirms published support" if primary == "BEARISH_REVERSAL" else "Range boundaries remain respected"]
    invalidation = ["Break below published support" if primary == "BULLISH_CONTINUATION" else "Break above published resistance" if primary == "BEARISH_REVERSAL" else "Confirmed breakout from range", "Published regime changes"]
    return {"symbol": _text(context.get("symbol")) or "UNKNOWN", "generated_at": generated_at, "cycle_id": context.get("cycle_id"), "status": status, "primary_scenario": primary, "primary_probability": probabilities[primary], "confidence": confidence, "alternative_scenarios": alternatives, "market_regime": _text(context.get("market_regime")) or "UNKNOWN", "trend_context": _number(context.get("trend_score")), "momentum_context": _number(context.get("momentum_score")), "structure_context": _number(context.get("structure_score")), "volatility_context": _number(context.get("atr")), "key_support": support, "key_resistance": resistance, "invalidation_conditions": invalidation, "confirmation_conditions": confirmations, "reasons": reasons, "blockers": blockers, "uncertainty_factors": ["Support/resistance unavailable"] if support is None or resistance is None else [], "data_quality": "PARTIAL" if support is None or resistance is None else "AVAILABLE", "source_freshness": generated_at}


def _read_rows(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [dict(item) for item in payload if isinstance(item, Mapping)] if isinstance(payload, list) else []
    except (OSError, ValueError, TypeError): return []


def _signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(key) for key in ("primary_scenario", "primary_probability", "confidence", "market_regime", "invalidation_conditions", "confirmation_conditions", "data_quality"))


def publish(contexts: list[Mapping[str, Any]], impulse_rows: list[Mapping[str, Any]] | None = None, *, output: Path = REPORT) -> list[dict[str, Any]]:
    if not _enabled(): return []
    _log("cycle_started", symbols=len(contexts))
    previous = {row.get("symbol"): row for row in _read_rows(output)}
    impulses = {row.get("symbol"): row for row in impulse_rows or [] if isinstance(row, Mapping)}
    rows = [evaluate(context, impulses.get(context.get("symbol"))) for context in contexts]
    rows.sort(key=lambda row: (row["primary_probability"] is None, -(row["primary_probability"] or 0), row["symbol"]))
    output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    last_history: dict[str, dict[str, Any]] = {}
    try:
        for line in HISTORY.read_text(encoding="utf-8").splitlines():
            if line.strip() and isinstance((item := json.loads(line)), dict): last_history[item.get("symbol")] = item
    except (OSError, ValueError, TypeError): pass
    if _history_enabled():
        with HISTORY.open("a", encoding="utf-8") as handle:
            for row in rows:
                if _signature(last_history.get(row["symbol"], {})) == _signature(row): continue
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    changes = []
    for row in rows:
        old = previous.get(row["symbol"])
        if old and (_signature(old) != _signature(row) and (old.get("primary_scenario") != row.get("primary_scenario") or abs((_number(old.get("primary_probability")) or 0) - (_number(row.get("primary_probability")) or 0)) >= _threshold() or old.get("confidence") != row.get("confidence") or old.get("market_regime") != row.get("market_regime") or old.get("invalidation_conditions") != row.get("invalidation_conditions") or old.get("confirmation_conditions") != row.get("confirmation_conditions"))):
            changes.append({"symbol": row["symbol"], "previous": old, "current": row, "generated_at": row["generated_at"]})
    CHANGES.write_text(json.dumps({"schema_version": "scenario-changes-v1", "generated_at": _now(), "status": "READY", "items": changes}, ensure_ascii=False, indent=2), encoding="utf-8")
    _log("cycle_finished", symbols_evaluated=len(rows), insufficient_data=sum(row["status"] == "INSUFFICIENT_DATA" for row in rows), material_changes=len(changes))
    return rows
