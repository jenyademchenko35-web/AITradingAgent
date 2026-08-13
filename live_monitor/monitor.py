"""Live Market Monitor service."""

from __future__ import annotations

import time
from typing import Any

from live_monitor.price_provider import PriceProvider
from live_monitor.service_health import monitor_status, utc_now
from live_monitor.state_manager import StateManager
from live_monitor.trade_tracker import TradeTracker, TrackedInstrument


class LiveMarketMonitor:
    """Read-only service that updates live_monitor_state.json."""

    def __init__(self, interval: int = 3, provider: str = "auto") -> None:
        self.interval = max(1, int(interval))
        self.provider_name = provider
        self.provider = PriceProvider(provider=provider)
        self.tracker = TradeTracker()
        self.state_manager = StateManager()
        self._last_trade_diagnostic: tuple[Any, ...] | None = None
        self._last_provider_error = ""
        self._last_provider_error_at = ""

    def run_forever(self) -> None:
        """Run the monitor loop until interrupted."""
        self.state_manager.log(f"{utc_now()} Live Monitor started interval={self.interval}")
        while True:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - service should stay alive
                self.state_manager.log(f"{utc_now()} ERROR {exc}")
            time.sleep(self.interval)

    def run_once(self) -> dict[str, Any]:
        """Collect targets, prices and persist state once."""
        cycle_started = time.monotonic()
        timestamp = utc_now()
        targets = self.tracker.collect_targets()
        self.log_trade_diagnostics()
        symbols = sorted({item.symbol for item in targets if item.symbol})
        quotes = self.provider.fetch_prices(symbols)
        items = []
        for target in targets:
            quote = quotes.get(target.symbol)
            if quote is None:
                items.append(self.unpriced_item(target, timestamp))
                continue
            items.append(self.tracker.enrich(target, quote))
        priced_count = sum(1 for item in items if item.get("price"))
        status = monitor_status(
            tracked_count=len(targets),
            priced_count=priced_count,
            fallback_used=self.provider.fallback_used,
        )
        provider_label = self.provider_label(quotes)
        last_error, last_error_at = self.provider_error(
            status=status,
            provider_label=provider_label,
            timestamp=timestamp,
        )
        history = self.state_manager.append_history(self.history_rows(timestamp, items))
        if history.error:
            self.state_manager.log(f"{timestamp} WARNING {history.error}")
        cache = self.tracker.cache_diagnostics
        state = {
            "generated_at": timestamp,
            "status": status,
            "provider": provider_label,
            "provider_mode": self.provider_name,
            "interval": self.interval,
            "symbols": symbols,
            "tracked_count": len(targets),
            "priced_count": priced_count,
            "fallback_used": self.provider.fallback_used,
            "last_error": last_error,
            "last_error_at": last_error_at,
            "items": items,
            "roles": self.role_counts(items),
            "telemetry": {
                "cycle_duration_ms": round((time.monotonic() - cycle_started) * 1000, 1),
                "tracked_symbols": len(symbols),
                "ticker_calls": len(symbols),
                "history_rows_appended": history.rows_appended,
                "history_compaction_performed": history.compaction_performed,
                "history_file_size_bytes": history.file_size_bytes,
                "source_cache_hits": cache["hits"],
                "source_cache_misses": cache["misses"],
            },
            "restrictions": [
                "Live Monitor read-only.",
                "DecisionEngine, сделки, SL/TP и торговая логика не меняются.",
            ],
        }
        self.state_manager.write_state(state)
        cycle_duration_ms = round((time.monotonic() - cycle_started) * 1000, 1)
        self.state_manager.log(
            f"{timestamp} status={status} provider={provider_label} "
            f"tracked={len(targets)} priced={priced_count} "
            f"cycle_ms={cycle_duration_ms} ticker_calls={len(symbols)} "
            f"history_appended={history.rows_appended} "
            f"history_compacted={history.compaction_performed} "
            f"history_bytes={history.file_size_bytes} "
            f"cache_hits={cache['hits']} cache_misses={cache['misses']}"
        )
        return state

    def log_trade_diagnostics(self) -> None:
        """Log the trade source and parsed count when their state changes."""
        diagnostic = self.tracker.trade_diagnostics
        signature = (
            diagnostic.get("source"),
            diagnostic.get("exists"),
            diagnostic.get("rows_found"),
            diagnostic.get("open_trades_loaded"),
            diagnostic.get("warning"),
        )
        if signature == self._last_trade_diagnostic:
            return
        self._last_trade_diagnostic = signature
        self.state_manager.log(f"INFO trades source={diagnostic.get('source')}")
        warning = str(diagnostic.get("warning", ""))
        if warning:
            self.state_manager.log(f"WARNING {warning}")
        self.state_manager.log(
            f"INFO open trades loaded={diagnostic.get('open_trades_loaded', 0)}"
        )

    def provider_error(
        self,
        status: str,
        provider_label: str,
        timestamp: str,
    ) -> tuple[str, str]:
        """Keep only a current provider error for Telegram diagnostics."""
        if status == "ONLINE" and provider_label == "REST":
            self._last_provider_error = ""
            self._last_provider_error_at = ""
            return "", ""
        error = str(self.provider.last_error or "").strip()
        if error and error != self._last_provider_error:
            self._last_provider_error = error
            self._last_provider_error_at = timestamp
        return self._last_provider_error, self._last_provider_error_at

    @staticmethod
    def unpriced_item(target: TrackedInstrument, timestamp: str) -> dict[str, Any]:
        """Return item when price is unavailable."""
        return {
            "symbol": target.symbol,
            "updated_at": timestamp,
            "role": target.role,
            "direction": target.direction,
            "entry": target.entry or "",
            "sl": target.sl or "",
            "tp": target.tp or "",
            "error": "Цена недоступна",
        }

    @staticmethod
    def provider_label(quotes: dict[str, Any]) -> str:
        """Return a compact provider label."""
        providers = sorted({quote.provider for quote in quotes.values() if quote})
        if not providers:
            return "N/A"
        return "+".join(providers)

    @staticmethod
    def role_counts(items: list[dict[str, Any]]) -> dict[str, int]:
        """Count tracked roles."""
        counts: dict[str, int] = {}
        for item in items:
            role = str(item.get("role", "UNKNOWN"))
            counts[role] = counts.get(role, 0) + 1
        return counts

    @staticmethod
    def history_rows(timestamp: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build price history rows."""
        rows = []
        for item in items:
            if not item.get("price"):
                continue
            rows.append(
                {
                    "timestamp": timestamp,
                    "symbol": item.get("symbol", ""),
                    "price": item.get("price", ""),
                    "role": item.get("role", ""),
                    "direction": item.get("direction", ""),
                    "entry": item.get("entry", ""),
                    "sl": item.get("sl", ""),
                    "tp": item.get("tp", ""),
                    "pnl_percent": item.get("pnl_percent", ""),
                    "pnl_usdt": item.get("pnl_usdt", ""),
                    "current_r": item.get("current_r", ""),
                    "distance_to_sl_percent": item.get("distance_to_sl_percent", ""),
                    "distance_to_tp_percent": item.get("distance_to_tp_percent", ""),
                }
            )
        return rows
