"""Historical price timeline and isolated scenario simulations."""

from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta
from typing import Any, Mapping

from market_intelligence_utils import OHLCVCache, parse_time, safe_float, symbol_full
from trade_replay_lab.replay_metrics import directional_return, price_r


TIMELINE_HORIZONS = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "8h": timedelta(hours=8),
    "24h": timedelta(hours=24),
}
ENTRY_OFFSETS = (-15, -5, 5, 15, 30)
ATR_VARIANTS = (1.0, 1.25, 1.5, 2.0)
TP_VARIANTS = (1.0, 1.5, 2.0, 3.0)


class ReplayScenarioAnalyzer:
    """Analyze trade alternatives exclusively from local 1h candles."""

    def __init__(self, ohlcv: OHLCVCache) -> None:
        self.ohlcv = ohlcv

    def analyze(self, trade: Mapping[str, Any]) -> dict[str, Any]:
        """Build timeline, path context and all supported scenarios."""
        symbol = symbol_full(str(trade.get("symbol", "")))
        opened_at = parse_time(trade.get("opened_at"))
        context = self.entry_context(symbol, opened_at)
        if not context.get("available"):
            return self.unavailable_scenarios(str(context.get("reason", "OHLCV недоступен")))

        index = int(context["index"])
        atr = self.ohlcv.atr(symbol, index)
        entry = safe_float(trade.get("entry"))
        stop = safe_float(trade.get("stop_loss") or trade.get("sl"))
        target = safe_float(trade.get("take_profit") or trade.get("tp"))
        direction = str(trade.get("direction", "")).upper()
        risk = abs(entry - stop)
        end_at = opened_at + timedelta(hours=24) if opened_at else None
        return {
            "ohlcv_available": True,
            "ohlcv_timeframe": "1h",
            "entry_candle_at": context.get("candle_at"),
            "atr": round(atr, 8),
            "timeline": self.timeline(symbol, opened_at, entry, direction),
            "path": self.path_metrics(symbol, index, direction, entry, risk, atr),
            "entry_timing_variants": self.entry_variants(),
            "atr_stop_variants": self.atr_stop_variants(
                symbol, index, direction, entry, target, atr, end_at
            ),
            "take_profit_variants": self.take_profit_variants(
                symbol, index, direction, entry, stop, risk, end_at
            ),
            "no_take_profit": self.no_take_profit(
                symbol, index, direction, entry, stop, risk
            ),
            "trailing_stop": self.trailing_stop(
                symbol, index, direction, entry, stop, atr, risk
            ),
        }

    def entry_context(self, symbol: str, opened_at: datetime | None) -> dict[str, Any]:
        """Return a recent entry candle or explain why replay is unavailable."""
        if opened_at is None:
            return {"available": False, "reason": "Время открытия сделки отсутствует."}
        index = self.ohlcv.index_at_or_before(symbol, opened_at)
        candles = self.ohlcv.load(symbol)
        if index is None or not candles:
            return {"available": False, "reason": "Локальный OHLCV cache отсутствует."}
        candle_time = candles[index]["timestamp"]
        if opened_at - candle_time > timedelta(hours=2):
            return {
                "available": False,
                "reason": "OHLCV cache не покрывает время открытия сделки.",
            }
        return {
            "available": True,
            "index": index,
            "candle_at": candle_time.isoformat(),
        }

    def timeline(
        self,
        symbol: str,
        opened_at: datetime | None,
        entry: float,
        direction: str,
    ) -> dict[str, Any]:
        """Build direction-aware post-entry movement at requested horizons."""
        candles = self.ohlcv.load(symbol)
        times = [candle["timestamp"] for candle in candles]
        result: dict[str, Any] = {}
        for label, delta in TIMELINE_HORIZONS.items():
            if delta < timedelta(hours=1):
                result[label] = {
                    "available": False,
                    "status": "DATA_UNAVAILABLE",
                    "reason": "В cache есть только свечи 1h.",
                }
                continue
            target = opened_at + delta if opened_at else None
            if target is None:
                result[label] = {"available": False, "status": "DATA_UNAVAILABLE"}
                continue
            index = bisect_left(times, target)
            if index >= len(candles) or candles[index]["timestamp"] - target > timedelta(minutes=90):
                result[label] = {
                    "available": False,
                    "status": "DATA_UNAVAILABLE",
                    "reason": "Свеча нужного горизонта отсутствует.",
                }
                continue
            price = safe_float(candles[index].get("close"))
            result[label] = {
                "available": price > 0,
                "timestamp": candles[index]["timestamp"].isoformat(),
                "price": price,
                "return_pct": directional_return(direction, entry, price),
                "favorable": directional_return(direction, entry, price) > 0,
            }
        return result

    def path_metrics(
        self,
        symbol: str,
        index: int,
        direction: str,
        entry: float,
        risk: float,
        atr: float,
    ) -> dict[str, Any]:
        """Describe impulse position and favorable/adverse movement."""
        candles = self.ohlcv.load(symbol)
        if not candles or entry <= 0:
            return {"available": False}
        before_index = max(0, index - 3)
        before_price = safe_float(candles[before_index].get("close"))
        pre_move = (
            (entry - before_price) if direction == "LONG" else (before_price - entry)
        )
        pre_move_atr = round(pre_move / atr, 4) if atr > 0 else 0.0
        future = candles[index + 1:index + 25]
        if direction == "LONG":
            favorable_price = max((safe_float(row.get("high")) for row in future), default=entry)
            adverse_price = min((safe_float(row.get("low")) for row in future), default=entry)
        else:
            favorable_price = min((safe_float(row.get("low")) for row in future), default=entry)
            adverse_price = max((safe_float(row.get("high")) for row in future), default=entry)
        max_favorable_r = price_r(direction, entry, favorable_price, risk) if risk else 0.0
        max_adverse_r = price_r(direction, entry, adverse_price, risk) if risk else 0.0
        first_close_r = (
            price_r(direction, entry, safe_float(future[0].get("close")), risk)
            if future and risk else 0.0
        )
        if pre_move_atr >= 1.5:
            phase = "END"
            phase_label = "в конце импульса"
        elif pre_move_atr >= 0.5:
            phase = "MIDDLE"
            phase_label = "в середине импульса"
        else:
            phase = "BEFORE"
            phase_label = "до импульса"
        return {
            "available": True,
            "pre_entry_move_atr": pre_move_atr,
            "momentum_phase": phase,
            "momentum_phase_label": phase_label,
            "late_entry": phase == "END",
            "max_favorable_r_24h": round(max_favorable_r, 4),
            "max_adverse_r_24h": round(max_adverse_r, 4),
            "immediate_reversal": first_close_r < -0.25,
            "first_hour_r": round(first_close_r, 4),
            "false_breakout": max_favorable_r < 0.5 and max_adverse_r <= -1.0,
        }

    @staticmethod
    def entry_variants() -> list[dict[str, Any]]:
        """Report minute variants as unavailable with 1h-only source data."""
        return [
            {
                "variant": f"Entry {offset:+d}m",
                "offset_minutes": offset,
                "available": False,
                "status": "DATA_UNAVAILABLE",
                "reason": "Для точного minute replay нужны свечи 5m или 1m; cache содержит 1h.",
            }
            for offset in ENTRY_OFFSETS
        ]

    def atr_stop_variants(
        self,
        symbol: str,
        index: int,
        direction: str,
        entry: float,
        original_target: float,
        atr: float,
        end_at: datetime | None,
    ) -> list[dict[str, Any]]:
        """Simulate isolated ATR Stop Loss variants with fixed target."""
        rows = []
        for multiplier in ATR_VARIANTS:
            if atr <= 0 or entry <= 0:
                rows.append(self.unavailable_variant(f"ATR {multiplier}", "ATR недоступен."))
                continue
            stop = entry - atr * multiplier if direction == "LONG" else entry + atr * multiplier
            target = original_target
            if target <= 0:
                target = entry + atr * multiplier * 2 if direction == "LONG" else entry - atr * multiplier * 2
            outcome = self.simulate_levels(
                symbol, index, direction, entry, stop, target, end_at
            )
            rows.append({
                "variant": f"ATR {multiplier:g}",
                "atr_multiplier": multiplier,
                "stop": round(stop, 8),
                "target": round(target, 8),
                **outcome,
            })
        return rows

    def take_profit_variants(
        self,
        symbol: str,
        index: int,
        direction: str,
        entry: float,
        stop: float,
        risk: float,
        end_at: datetime | None,
    ) -> list[dict[str, Any]]:
        """Simulate isolated Take Profit variants with fixed Stop Loss."""
        rows = []
        for multiple in TP_VARIANTS:
            if risk <= 0 or stop <= 0:
                rows.append(self.unavailable_variant(f"TP {multiple:g}R", "Risk/SL недоступен."))
                continue
            target = entry + risk * multiple if direction == "LONG" else entry - risk * multiple
            outcome = self.simulate_levels(
                symbol, index, direction, entry, stop, target, end_at
            )
            rows.append({
                "variant": f"TP {multiple:g}R",
                "r_multiple": multiple,
                "stop": round(stop, 8),
                "target": round(target, 8),
                **outcome,
            })
        return rows

    def simulate_levels(
        self,
        symbol: str,
        index: int,
        direction: str,
        entry: float,
        stop: float,
        target: float,
        end_at: datetime | None,
    ) -> dict[str, Any]:
        """Simulate fixed SL/TP from the first complete candle after entry."""
        candles = self.ohlcv.load(symbol)
        risk = abs(entry - stop)
        if not candles or risk <= 0 or index + 1 >= len(candles):
            return self.unavailable_variant_data("Нет будущих OHLCV-свечей.")
        considered = []
        for candle in candles[index + 1:index + 25]:
            if end_at and candle["timestamp"] > end_at:
                break
            considered.append(candle)
            if direction == "LONG":
                stop_hit = safe_float(candle.get("low")) <= stop
                target_hit = safe_float(candle.get("high")) >= target
            else:
                stop_hit = safe_float(candle.get("high")) >= stop
                target_hit = safe_float(candle.get("low")) <= target
            if stop_hit:
                return {
                    "available": True,
                    "outcome": "LOSS",
                    "outcome_r": -1.0,
                    "hit_at": candle["timestamp"].isoformat(),
                    "intrabar_policy": "SL first when SL and TP touch the same 1h candle",
                }
            if target_hit:
                return {
                    "available": True,
                    "outcome": "WIN",
                    "outcome_r": price_r(direction, entry, target, risk),
                    "hit_at": candle["timestamp"].isoformat(),
                }
        if not considered:
            return self.unavailable_variant_data("Нет свечей в окне replay.")
        last = considered[-1]
        final_price = safe_float(last.get("close"))
        return {
            "available": True,
            "outcome": "NEITHER",
            "outcome_r": price_r(direction, entry, final_price, risk),
            "hit_at": "",
            "final_price": final_price,
            "final_at": last["timestamp"].isoformat(),
        }

    def no_take_profit(
        self,
        symbol: str,
        index: int,
        direction: str,
        entry: float,
        stop: float,
        risk: float,
    ) -> dict[str, Any]:
        """Exit after a two-close reversal, with original SL still active."""
        candles = self.ohlcv.load(symbol)
        future = candles[index + 1:index + 25]
        if not future or risk <= 0:
            return self.unavailable_variant_data("Нет будущих свечей или Risk недоступен.")
        previous_r = 0.0
        reversal_count = 0
        favorable_seen = False
        for candle in future:
            if direction == "LONG" and safe_float(candle.get("low")) <= stop:
                return {"available": True, "outcome": "LOSS", "outcome_r": -1.0, "exit_rule": "SL"}
            if direction == "SHORT" and safe_float(candle.get("high")) >= stop:
                return {"available": True, "outcome": "LOSS", "outcome_r": -1.0, "exit_rule": "SL"}
            current_r = price_r(direction, entry, safe_float(candle.get("close")), risk)
            favorable_seen = favorable_seen or current_r >= 0.5
            reversal_count = reversal_count + 1 if favorable_seen and current_r < previous_r else 0
            if reversal_count >= 2:
                return {
                    "available": True,
                    "outcome": "REVERSAL_EXIT",
                    "outcome_r": current_r,
                    "exit_at": candle["timestamp"].isoformat(),
                    "exit_rule": "два закрытия против favorable movement",
                }
            previous_r = current_r
        final_r = price_r(direction, entry, safe_float(future[-1].get("close")), risk)
        return {
            "available": True,
            "outcome": "TIME_EXIT",
            "outcome_r": final_r,
            "exit_at": future[-1]["timestamp"].isoformat(),
        }

    def trailing_stop(
        self,
        symbol: str,
        index: int,
        direction: str,
        entry: float,
        original_stop: float,
        atr: float,
        risk: float,
    ) -> dict[str, Any]:
        """Simulate a simple 1 ATR trail activated after +1 ATR."""
        candles = self.ohlcv.load(symbol)
        future = candles[index + 1:index + 25]
        if not future or atr <= 0 or risk <= 0:
            return self.unavailable_variant_data("Нет будущих свечей, ATR или Risk.")
        trail = original_stop
        activated = False
        extreme = entry
        for candle in future:
            high = safe_float(candle.get("high"))
            low = safe_float(candle.get("low"))
            if direction == "LONG":
                extreme = max(extreme, high)
                if extreme - entry >= atr:
                    activated = True
                    trail = max(trail, extreme - atr)
                if low <= trail:
                    return {
                        "available": True,
                        "outcome": "TRAIL_EXIT" if activated else "LOSS",
                        "outcome_r": price_r(direction, entry, trail, risk),
                        "exit_at": candle["timestamp"].isoformat(),
                        "activated": activated,
                    }
            else:
                extreme = min(extreme, low)
                if entry - extreme >= atr:
                    activated = True
                    trail = min(trail, extreme + atr)
                if high >= trail:
                    return {
                        "available": True,
                        "outcome": "TRAIL_EXIT" if activated else "LOSS",
                        "outcome_r": price_r(direction, entry, trail, risk),
                        "exit_at": candle["timestamp"].isoformat(),
                        "activated": activated,
                    }
        final_r = price_r(direction, entry, safe_float(future[-1].get("close")), risk)
        return {
            "available": True,
            "outcome": "TIME_EXIT",
            "outcome_r": final_r,
            "activated": activated,
        }

    @staticmethod
    def unavailable_variant(name: str, reason: str) -> dict[str, Any]:
        return {
            "variant": name,
            "available": False,
            "status": "DATA_UNAVAILABLE",
            "reason": reason,
        }

    @staticmethod
    def unavailable_variant_data(reason: str) -> dict[str, Any]:
        return {"available": False, "status": "DATA_UNAVAILABLE", "reason": reason}

    def unavailable_scenarios(self, reason: str) -> dict[str, Any]:
        """Return a complete shape when OHLCV does not cover a trade."""
        unavailable = self.unavailable_variant_data(reason)
        return {
            "ohlcv_available": False,
            "ohlcv_timeframe": "1h",
            "atr": 0.0,
            "timeline": {
                label: {**unavailable}
                for label in TIMELINE_HORIZONS
            },
            "path": {**unavailable},
            "entry_timing_variants": self.entry_variants(),
            "atr_stop_variants": [
                self.unavailable_variant(f"ATR {value:g}", reason)
                for value in ATR_VARIANTS
            ],
            "take_profit_variants": [
                self.unavailable_variant(f"TP {value:g}R", reason)
                for value in TP_VARIANTS
            ],
            "no_take_profit": {**unavailable},
            "trailing_stop": {**unavailable},
        }
