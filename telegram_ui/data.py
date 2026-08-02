"""Read-only adapters from persisted production rows to UI contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .models import SignalCardPayload, build_signal_fingerprint


def _float(value: Any) -> float | None:
    if value in (None, "", "N/A", "None"):
        return None
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return None


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _items(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    text = str(value or "").strip()
    if not text:
        return ()
    separator = " | " if " | " in text else ";" if ";" in text else ","
    return tuple(part.strip() for part in text.split(separator) if part.strip())


def normalize_symbol(value: Any) -> str:
    clean = str(value or "").strip().upper().replace("-", "/")
    compact = clean.replace("/", "")
    return f"{compact[:-4]}/USDT" if compact.endswith("USDT") else clean


def compact_symbol(value: Any) -> str:
    return normalize_symbol(value).replace("/", "")


def available_timeframes(rows: Iterable[Mapping[str, Any]], symbol: str) -> tuple[str, ...]:
    target = normalize_symbol(symbol)
    found = {
        str(row.get("timeframe") or "1h").lower()
        for row in rows
        if normalize_symbol(row.get("symbol")) == target
    }
    order = ("15m", "1h", "4h", "1d")
    return tuple(value for value in order if value in found)


def latest_rows(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for source in rows:
        row = dict(source)
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        timeframe = str(row.get("timeframe") or "1h").lower()
        key = symbol, timeframe
        if key not in latest or str(row.get("timestamp", "")) >= str(latest[key].get("timestamp", "")):
            latest[key] = row
    return latest


def latest_symbols(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    return sorted({symbol for symbol, _ in latest_rows(rows)})


def _direction(long_value: Any, short_value: Any) -> str:
    long_score, short_score = _float(long_value), _float(short_value)
    if long_score is None or short_score is None:
        return ""
    if long_score > short_score:
        return "BULLISH"
    if short_score > long_score:
        return "BEARISH"
    return "NEUTRAL"


def _component_scores(row: Mapping[str, Any]) -> tuple[tuple[str, float, float | None], ...]:
    output = []
    for label, aliases, maximum_aliases in (
        ("Trend", ("trend_score",), ("trend_max",)),
        ("Momentum", ("momentum_score",), ("momentum_max",)),
        ("Structure", ("structure_score",), ("structure_max",)),
        ("Risk", ("risk_score",), ("risk_max",)),
    ):
        value = _float(_first(row, *aliases))
        if value is None:
            long_value = _float(row.get(label.lower() + "_long"))
            short_value = _float(row.get(label.lower() + "_short"))
            candidates = [item for item in (long_value, short_value) if item is not None]
            value = max(candidates) if candidates else None
        if value is not None:
            output.append((label, value, _float(_first(row, *maximum_aliases))))
    return tuple(output)


def signal_payload_from_rows(
    decision_row: Mapping[str, Any],
    *,
    trade_row: Mapping[str, Any] | None = None,
    timeframe: str = "1h",
) -> SignalCardPayload:
    """Map saved values without synthesizing a trading plan."""
    row = dict(decision_row)
    trade = dict(trade_row or {})
    symbol = normalize_symbol(_first(row, "symbol") or trade.get("symbol"))
    side = str(_first(row, "direction", "side") or _first(trade, "direction", "side") or "NEUTRAL").upper()
    status = str(_first(row, "signal", "decision", "status") or "NO TRADE").upper().replace("_", " ")
    entry = _float(_first(row, "entry", "entry_price") or _first(trade, "entry", "entry_price"))
    stop = _float(_first(row, "stop_loss", "sl") or _first(trade, "stop_loss", "sl"))
    target = _float(_first(row, "take_profit", "tp") or _first(trade, "take_profit", "tp"))
    current = _float(_first(row, "current_price", "price", "close") or _first(trade, "current_price"))
    risk = abs(entry - stop) if entry is not None and stop is not None else None
    reward = abs(target - entry) if entry is not None and target is not None else None
    rr = reward / risk if risk not in (None, 0) and reward is not None else _float(row.get("risk_reward"))
    risk_percent = risk / abs(entry) * 100 if risk is not None and entry not in (None, 0) else None
    target_percent = reward / abs(entry) * 100 if reward is not None and entry not in (None, 0) else None
    timestamp = str(_first(row, "timestamp", "decision_timestamp") or datetime.now(timezone.utc).isoformat())
    fingerprint = str(row.get("signal_fingerprint") or build_signal_fingerprint(
        symbol=symbol, side=side, timeframe=timeframe, entry=entry,
        stop_loss=stop, take_profit=target, status=status,
    ))
    reasons = _items(_first(row, "reasons", "confirmations", "summary"))
    blockers = _items(_first(row, "blockers", "failed_filters", "veto_reasons"))
    return SignalCardPayload(
        symbol=symbol, side=side, status=status, timeframe=timeframe,
        strategy_id=str(row.get("strategy_id") or "LIVE_BASELINE"),
        current_price=current, entry=entry, stop_loss=stop, take_profit=target,
        risk_reward=rr, risk_percent=risk_percent, target_percent=target_percent,
        confidence=_float(row.get("confidence")) or 0.0,
        quality=str(row.get("quality") or "N/A"),
        score=_float(_first(row, "score", "signal_score", "weighted_score")) or 0.0,
        blockers=blockers, reasons=reasons,
        trend_1h=str(row.get("trend_1h") or _direction(row.get("trend_long"), row.get("trend_short"))),
        trend_4h=str(row.get("trend_4h") or ""), trend_1d=str(row.get("trend_1d") or ""),
        timestamp=timestamp, cycle_id=str(row.get("cycle_id") or ""),
        snapshot_id=str(row.get("snapshot_id") or ""), signal_fingerprint=fingerprint,
        component_scores=_component_scores(row), adx=_float(row.get("adx")),
        atr_percent=_float(_first(row, "atr_percent", "atr_pct")),
        market_regime=str(row.get("market_regime") or ""),
        confirmations=_items(row.get("confirmations")),
        limitations=_items(row.get("limitations")),
    )
