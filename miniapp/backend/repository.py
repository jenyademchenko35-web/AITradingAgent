"""Read-only projection of existing immutable AITradingAgent artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
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


@dataclass(frozen=True)
class _SnapshotCacheEntry:
    signature: tuple[int, int] | None
    expires_at: float
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _RuntimeJsonCacheEntry:
    signature: tuple[int, int] | None
    expires_at: float
    payload: Mapping[str, Any]

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
        self.cache_ttl_seconds = max(0.1, float(cache_ttl_seconds))
        self.max_source_rows = max(100, int(max_source_rows))
        self.similar_min_sample = max(1, int(similar_min_sample))
        self._cache = MTimeCSVCache(
            ttl_seconds=cache_ttl_seconds, max_rows=self.max_source_rows,
            timeout_seconds=self.query_timeout_seconds,
        )
        self._snapshot_cache: _SnapshotCacheEntry | None = None
        self._snapshot_lock = threading.RLock()
        self._runtime_json_cache: dict[str, _RuntimeJsonCacheEntry] = {}
        self._runtime_json_lock = threading.RLock()
        self._max_snapshot_bytes = max(
            1_048_576, min(33_554_432, self.max_source_rows * 4096),
        )

    def read_csv(self, path: str | Path) -> list[dict[str, str]]:
        return self._cache.read(path)

    def _runtime_json(self, filename: str) -> dict[str, Any]:
        """Read one bounded, published runtime object without writing or importing runtime code."""
        path = self.base_dir / filename
        try:
            stat = path.stat()
            signature: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        now = time.monotonic()
        with self._runtime_json_lock:
            cached = self._runtime_json_cache.get(filename)
            if cached and cached.signature == signature and now < cached.expires_at:
                return dict(cached.payload)

        payload: Mapping[str, Any] = {}
        if signature is not None and signature[1] <= self._max_snapshot_bytes:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    raw = handle.read(self._max_snapshot_bytes + 1)
                if len(raw.encode("utf-8")) <= self._max_snapshot_bytes:
                    decoded = json.loads(raw)
                    if isinstance(decoded, Mapping):
                        payload = decoded
            except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                payload = {}
        frozen = dict(payload)
        with self._runtime_json_lock:
            self._runtime_json_cache[filename] = _RuntimeJsonCacheEntry(
                signature=signature, expires_at=now + self.cache_ttl_seconds, payload=frozen,
            )
        return dict(frozen)

    @staticmethod
    def _published_value(*values: Any) -> Any:
        for value in values:
            if value not in (None, "", "N/A", "NEVER"):
                return value
        return None

    @staticmethod
    def _adapt_signal_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
        adapted: list[dict[str, Any]] = []
        for source in rows:
            row: dict[str, Any] = dict(source)
            timeframe = str(row.get("timeframe") or "1h").lower()
            if timeframe == "multi":
                row["source_timeframe"] = timeframe
                row["timeframe"] = "1h"
            adapted.append(row)
        return adapted

    def _snapshot_rows(self) -> list[dict[str, Any]]:
        path = self.base_dir / "decision_snapshot.json"
        try:
            stat = path.stat()
            signature: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        now = time.monotonic()
        with self._snapshot_lock:
            cached = self._snapshot_cache
            if cached and cached.signature == signature and now < cached.expires_at:
                return [dict(row) for row in cached.rows]

        rows: list[dict[str, Any]] = []
        deadline = now + self.query_timeout_seconds
        if signature is not None and signature[1] <= self._max_snapshot_bytes:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    raw = handle.read(self._max_snapshot_bytes + 1)
                if len(raw.encode("utf-8")) <= self._max_snapshot_bytes and time.monotonic() <= deadline:
                    payload = json.loads(raw)
                    if isinstance(payload, Mapping):
                        from .intelligence import decision_snapshot_rows
                        rows = decision_snapshot_rows(payload, max_rows=self.max_source_rows)
            except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                rows = []
        frozen = tuple(dict(row) for row in rows)
        with self._snapshot_lock:
            self._snapshot_cache = _SnapshotCacheEntry(
                signature=signature,
                expires_at=now + self.cache_ttl_seconds,
                rows=frozen,
            )
        return [dict(row) for row in frozen]

    def decision_rows(self) -> list[dict[str, Any]]:
        snapshots = self._snapshot_rows()
        if snapshots:
            return snapshots
        for filename in ("signals_v3.csv", "signals.csv"):
            rows = self.read_csv(self.base_dir / filename)
            if rows:
                return self._adapt_signal_rows(rows)
        return self.read_csv(self.base_dir / "decision_debug.csv")

    def trade_rows(self) -> list[dict[str, str]]:
        return self.read_csv(self.base_dir / "trades.csv")

    def diagnostics(self) -> dict[str, Any]:
        """Read pre-generated diagnostics only; this API never triggers analysis or writes."""
        names = ("signal_episode_report.json", "blocker_statistics.json", "trade_quality_report.json", "symbol_statistics.json", "direction_bias.json", "feature_importance.json", "strategy_recommendations.json")
        return {name.removesuffix(".json"): self._runtime_json(name) for name in names}

    def updated_at(self) -> str:
        paths = [
            self.base_dir / "signals.csv", self.base_dir / "decision_snapshot.json",
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

    def system(self) -> dict[str, Any]:
        """Safe projection of already-published runtime status; no process inspection."""
        dashboard_state = self._runtime_json("dashboard_state.json")
        live_monitor = self._runtime_json("live_monitor_state.json")
        agent_stats = self._runtime_json("agent_v3_stats.json")
        research_status = self._runtime_json("research_lab_v2_status.json")
        system_status = dashboard_state.get("system")
        trading = dashboard_state.get("trading")
        dashboard_live = dashboard_state.get("live_monitor")
        dashboard_research = dashboard_state.get("strategy_lab")
        telegram = dashboard_state.get("telegram")
        news = dashboard_state.get("news")
        safe_system = system_status if isinstance(system_status, Mapping) else {}
        safe_trading = trading if isinstance(trading, Mapping) else {}
        safe_dashboard_live = dashboard_live if isinstance(dashboard_live, Mapping) else {}
        safe_dashboard_research = dashboard_research if isinstance(dashboard_research, Mapping) else {}
        safe_telegram = telegram if isinstance(telegram, Mapping) else {}
        safe_news = news if isinstance(news, Mapping) else {}
        has_agent_runtime = any((dashboard_state, live_monitor, agent_stats))
        research = self._published_value(
            research_status.get("status"),
            "ONLINE" if research_status.get("enabled") is True else None,
            "OFF" if research_status.get("enabled") is False else None,
            safe_dashboard_research.get("status"),
        ) or "UNKNOWN"
        return {
            "server": self._published_value(safe_system.get("status"), live_monitor.get("status")),
            "agent": "ONLINE" if has_agent_runtime else None,
            "telegram": self._published_value(safe_telegram.get("status")),
            "research": research,
            "news": self._published_value(safe_news.get("status")),
            "cycle": self._published_value(
                safe_trading.get("last_cycle"), safe_trading.get("cycle"),
                live_monitor.get("cycle"), live_monitor.get("current_cycle"),
                live_monitor.get("last_cycle"), agent_stats.get("runs"),
            ),
            "interval_seconds": self._published_value(
                live_monitor.get("interval"), safe_dashboard_live.get("interval"),
                agent_stats.get("interval_seconds"),
            ),
            "next_cycle_seconds": self._published_value(
                safe_trading.get("next_cycle_seconds"), live_monitor.get("next_cycle_seconds"),
                agent_stats.get("next_cycle_seconds"),
            ),
            "last_cycle_timestamp": self._published_value(
                safe_trading.get("last_cycle"), live_monitor.get("generated_at"),
                dashboard_state.get("generated_at"),
            ),
            "uptime_seconds": self._published_value(
                safe_system.get("uptime_seconds"), safe_trading.get("uptime_seconds"),
                live_monitor.get("uptime_seconds"), agent_stats.get("uptime_seconds"),
            ),
            "read_only": True,
        }

    def activity(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return only timestamped, saved signal rows; never fabricate history."""
        events: list[dict[str, Any]] = []
        for row in self.decision_rows()[-max(1, min(limit, 100)):]:
            timestamp = row.get("timestamp") or row.get("updated_at")
            if not timestamp:
                continue
            symbol = row.get("symbol")
            status = row.get("signal") or row.get("decision") or row.get("status")
            events.append({
                "timestamp": str(timestamp), "type": "signal_snapshot",
                "symbol": str(symbol) if symbol else None,
                "timeframe": str(row.get("timeframe")) if row.get("timeframe") else None,
                "status": str(status) if status else None,
            })
        return events[-max(1, min(limit, 100)):]

    def shadow(self) -> dict[str, Any]:
        """Expose Research Lab's existing separate shadow ledger without aggregation."""
        report = self.research()
        ledger = report.get("shadow_ledger")
        safe_ledger = ledger if isinstance(ledger, Mapping) else {}
        runtime = report.get("runtime_status")
        safe_runtime = runtime if isinstance(runtime, Mapping) else {}
        return {
            "active": safe_ledger.get("open", []),
            "closed": safe_ledger.get("closed", []),
            "strategies": safe_runtime.get("strategy_modes", []),
            "symbols": None,
            "updated": safe_runtime.get("updated_at"),
        }

    def research_live(self) -> dict[str, Any]:
        """Read-only subset of the existing ResearchDashboardV2 report."""
        report = self.research()
        return {
            "runtime_status": report.get("runtime_status"),
            "best_candidate": report.get("best_candidate") or None,
            "promotion_probability": report.get("promotion_probability"),
            "ranking": report.get("top_strategies", []),
            "recommendation": report.get("recommendation"),
            "top_features": report.get("top_features", []),
            "worst_features": report.get("worst_features", []),
        }

    def health_checks(self) -> dict[str, bool]:
        """Filesystem availability only; paths and payloads are deliberately omitted."""
        def readable(filename: str) -> bool:
            path = self.base_dir / filename
            return path.is_file() and os.access(path, os.R_OK)

        return {
            "repository": self.base_dir.is_dir() and os.access(self.base_dir, os.R_OK),
            "signals": any(readable(name) for name in (
                "decision_snapshot.json", "signals_v3.csv", "signals.csv", "decision_debug.csv",
            )),
            "watchlist": any(readable(name) for name in (
                "decision_snapshot.json", "signals_v3.csv", "signals.csv", "decision_debug.csv",
            )),
            "research": readable("research.db"),
            "snapshot": readable("decision_snapshot.json"),
        }

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
