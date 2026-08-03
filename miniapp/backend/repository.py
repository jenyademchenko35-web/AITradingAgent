"""Read-only projection of existing immutable AITradingAgent artifacts."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Mapping

from research_lab_v2.dashboard import ResearchDashboardV2
from telegram_ui.data import (
    available_timeframes,
    latest_rows,
    normalize_symbol,
    signal_payload_from_rows,
)
from trade_metrics_normalizer import aggregate_trade_metrics, is_closed_trade

from .cache import MTimeCSVCache

def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class ReadOnlyRepository:
    def __init__(self, base_dir: str | Path, *, cache_ttl_seconds: float = 5,
                 query_timeout_seconds: float = 2.0, max_source_rows: int = 10_000,
                 similar_min_sample: int = 20) -> None:
        self.base_dir = Path(base_dir)
        self.query_timeout_seconds = max(0.1, float(query_timeout_seconds))
        self.max_source_rows = max(100, int(max_source_rows))
        self.similar_min_sample = max(1, int(similar_min_sample))
        self._cache = MTimeCSVCache(
            ttl_seconds=cache_ttl_seconds, max_rows=self.max_source_rows,
            timeout_seconds=self.query_timeout_seconds,
        )

    def read_csv(self, path: str | Path) -> list[dict[str, str]]:
        return self._cache.read(path)

    def decision_rows(self) -> list[dict[str, str]]:
        return self.read_csv(self.base_dir / "decision_debug.csv") or self.read_csv(self.base_dir / "signals_v3.csv")

    def trade_rows(self) -> list[dict[str, str]]:
        return self.read_csv(self.base_dir / "trades.csv")

    def updated_at(self) -> str:
        paths = [
            self.base_dir / "decision_debug.csv", self.base_dir / "signals_v3.csv",
            self.base_dir / "trades.csv", self.base_dir / "research.db",
        ]
        mtimes = [path.stat().st_mtime for path in paths if path.exists()]
        stamp = max(mtimes) if mtimes else datetime.now(timezone.utc).timestamp()
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat()

    def watchlist(self) -> list[dict[str, Any]]:
        latest = latest_rows(self.decision_rows())
        items = []
        for (symbol, timeframe), row in sorted(latest.items()):
            items.append({
                "symbol": symbol,
                "status": str(row.get("signal") or row.get("decision") or "NO TRADE").upper(),
                "side": str(row.get("direction") or row.get("side") or "NEUTRAL").upper(),
                "confidence": _number(row.get("confidence")) or 0,
                "quality": str(row.get("quality") or "N/A"),
                "score": _number(row.get("score") or row.get("weighted_score")) or 0,
                "timeframe": timeframe,
                "updated_at": str(row.get("timestamp") or ""),
            })
        return items

    def _matching_trade(self, row: Mapping[str, Any], symbol: str, timeframe: str) -> dict[str, Any] | None:
        matching = [item for item in self.trade_rows() if (
            normalize_symbol(item.get("symbol")) == symbol
            and str(item.get("timeframe") or "1h").lower() == timeframe
        )]
        fingerprint, cycle_id = str(row.get("signal_fingerprint") or ""), str(row.get("cycle_id") or "")
        exact = [item for item in matching if (
            (fingerprint and item.get("signal_fingerprint") == fingerprint)
            or (cycle_id and item.get("cycle_id") == cycle_id)
        )]
        opened = [item for item in matching if str(item.get("status", "")).upper() == "OPEN"]
        return (opened or exact)[-1] if (opened or exact) else None

    def _candles(self, symbol: str, timeframe: str) -> tuple[dict[str, Any], ...]:
        path = self.base_dir / "ohlcv_cache" / f"{symbol.replace('/', '_')}_{timeframe}.csv"
        candles = []
        for row in self.read_csv(path)[-500:]:
            values = {key: _number(row.get(key)) for key in ("open", "high", "low", "close", "volume")}
            if any(values[key] is None for key in ("open", "high", "low", "close")):
                continue
            candles.append({"time": row.get("timestamp", ""), **values})
        return tuple(candles)

    def signal(self, symbol_value: str, timeframe: str | None = None) -> dict[str, Any] | None:
        symbol = normalize_symbol(symbol_value)
        if not re.fullmatch(r"[A-Z0-9]{2,20}/USDT", symbol):
            return None
        rows = self.decision_rows()
        timeframes = available_timeframes(rows, symbol)
        selected = (timeframe or ("1h" if "1h" in timeframes else timeframes[0] if timeframes else "1h")).lower()
        if selected not in {"15m", "1h", "4h", "1d"}:
            return None
        row = latest_rows(rows).get((symbol, selected))
        if row is None:
            return None
        payload = signal_payload_from_rows(
            row, trade_row=self._matching_trade(row, symbol, selected), timeframe=selected,
        )
        payload_data = asdict(payload)
        targets = {
            "tp1": payload.take_profit,
            "tp2": _number(row.get("take_profit_2") or row.get("tp2")),
            "tp3": _number(row.get("take_profit_3") or row.get("tp3")),
        }
        return {
            "symbol": symbol, "timeframe": selected,
            "available_timeframes": timeframes, "payload": payload_data,
            "targets": targets, "candles": self._candles(symbol, selected),
        }

    def open_trades(self) -> list[dict[str, str]]:
        return [row for row in self.trade_rows() if str(row.get("status", "")).upper() == "OPEN"]

    def trade_history(self) -> list[dict[str, str]]:
        return [row for row in self.trade_rows() if str(row.get("status", "")).upper() != "OPEN"]

    def stats(self) -> dict[str, Any]:
        closed = [row for row in self.trade_rows() if is_closed_trade(row)]
        return aggregate_trade_metrics(closed)

    def research(self) -> dict[str, Any]:
        return ResearchDashboardV2(self.base_dir / "research.db").build_report()

    def dashboard(self) -> dict[str, Any]:
        metrics = self.stats()
        research = self.research()
        runtime = research.get("runtime_status", {})
        return {
            "status": "ONLINE", "updated_at": self.updated_at(),
            "open_trades": len(self.open_trades()),
            "winrate": float(metrics.get("winrate", 0)),
            "profit_factor": float(metrics.get("profit_factor", 0)),
            "research_status": "ON" if runtime.get("enabled") else "OFF",
        }

    def signal_intelligence(self, symbol: str, timeframe: str):
        from .intelligence import SignalIntelligenceService
        return SignalIntelligenceService(self).intelligence(symbol, timeframe)

    def signal_history(self, symbol: str, timeframe: str, *, page: int = 1, page_size: int = 50):
        from .intelligence import SignalIntelligenceService
        return SignalIntelligenceService(self).history(symbol, timeframe, page=page, page_size=page_size)

    def signal_changes(self, symbol: str, timeframe: str):
        from .intelligence import SignalIntelligenceService
        return SignalIntelligenceService(self).changes(symbol, timeframe)

    def signal_requirements(self, symbol: str, timeframe: str):
        from .intelligence import SignalIntelligenceService
        return SignalIntelligenceService(self).requirements(symbol, timeframe)

    def similar_setups(self, symbol: str, timeframe: str, *, source: str = "LIVE",
                       page: int = 1, page_size: int = 20):
        from .intelligence import SignalIntelligenceService
        return SignalIntelligenceService(self).similar(
            symbol, timeframe, source=source, page=page, page_size=page_size,
        )
