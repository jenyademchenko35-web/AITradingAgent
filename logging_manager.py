"""Console output manager for AITradingAgent."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from best_candidate_ranker import explain_selection, rank_candidates


LEVELS = {
    "QUIET": 0,
    "NORMAL": 1,
    "VERBOSE": 2,
    "DEBUG": 3,
}


class ConsoleOutputManager:
    """Manage console output profiles for the trading agent."""

    def __init__(self, log_level: str = "NORMAL") -> None:
        normalized = str(log_level or "NORMAL").upper()
        self.log_level = normalized if normalized in LEVELS else "NORMAL"

    def allows(self, level: str) -> bool:
        """Return whether the configured level includes the requested level."""
        return LEVELS[self.log_level] >= LEVELS[level]

    def _print(self, *lines: str) -> None:
        for line in lines:
            print(line)

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%H:%M:%S")

    def timestamped(self, message: str, minimum: str = "NORMAL") -> None:
        """Print a timestamped message when the log level allows it."""
        if self.allows(minimum):
            self._print(f"[{self._timestamp()}] {message}")

    def startup(self, started_at: str) -> None:
        self._print("=" * 60, f"Multi-Timeframe Agent v3 started at {started_at}", "=" * 60)

    def cycle_started(self, started_at: str) -> None:
        self._print("=" * 60, f"Cycle started at {started_at}", "=" * 60)

    def cycle_finished(self, duration: float) -> None:
        self._print(f"Cycle duration: {duration:.2f} seconds")

    def sleeping(self, interval_seconds: int) -> None:
        self._print(f"Sleeping {interval_seconds} seconds...")

    def stopping(self) -> None:
        self._print("\nStopping agent...")

    def loop_error(self, error: Exception) -> None:
        self._print(f"[LOOP ERROR] {error}")

    def symbol_error(self, symbol: str, error: Exception) -> None:
        self.timestamped(f"[ERROR] {symbol}: {error}", minimum="QUIET")

    def symbol_summary(self, symbol: str, decision: Any, elapsed: float) -> None:
        if not self.allows("NORMAL"):
            return
        line = (
            f"{symbol} | {decision.direction} | {decision.signal} | "
            f"Score={decision.score} | Confidence={decision.confidence}%"
        )
        if self.allows("DEBUG"):
            line += f" | Quality={decision.quality} | {decision.summary} ({elapsed:.2f}s)"
        self._print(line)

    def analysis_reports(self, xai_text: str, diagnostics_text: str) -> None:
        if self.allows("VERBOSE"):
            self._print(xai_text, diagnostics_text)

    def cooldown_active(self, symbol: str, setup_id: str) -> None:
        self.timestamped(f"[{symbol}] Cooldown active for {setup_id}", minimum="NORMAL")

    def open_trade_exists(self, symbol: str) -> None:
        self.timestamped(f"[{symbol}] Open trade already exists, skipping.", minimum="NORMAL")

    def higher_tf_rejected(self, symbol: str, direction: str) -> None:
        self.timestamped(
            f"[{symbol}] {direction} rejected by higher timeframe trend filter.",
            minimum="NORMAL",
        )

    def notification_sending(self, symbol: str) -> None:
        self.timestamped(f"Sending notification for {symbol}", minimum="NORMAL")

    def notification_sent(self) -> None:
        self.timestamped("Notification sent", minimum="NORMAL")

    def no_symbols_analyzed(self) -> None:
        self._print("No symbols analyzed this cycle.")

    def best_setup(self, symbol: str, decision: Any) -> None:
        candidate = rank_candidates([(symbol, decision)])[0]
        self._print(
            "\nBEST CANDIDATE:",
            "=" * 60,
            (
                f"{candidate.symbol}: {candidate.direction} | "
                f"{candidate.decision} | Status={candidate.status} | "
                f"Quality={decision.quality} | Score={decision.score} | "
                f"Confidence={candidate.confidence}% | "
                f"Weighted Score={candidate.weighted_score:g} | "
                f"Edge={candidate.edge:g}"
            ),
            "Причина:",
            explain_selection(candidate),
            "=" * 60,
        )

    def ranked_summary(self, decisions: Iterable[tuple[str, Any]]) -> None:
        if not self.allows("NORMAL"):
            return
        decision_items = list(decisions)
        ranked = rank_candidates(decision_items)
        self._print("Ranked summary by candidate quality:", "=" * 60)
        decision_by_symbol = {symbol: decision for symbol, decision in decision_items}
        for candidate in ranked:
            decision = decision_by_symbol.get(candidate.symbol)
            quality = getattr(decision, "quality", "") if decision else ""
            self._print(
                f"{candidate.symbol}: {candidate.direction} | "
                f"{candidate.decision} | Status={candidate.status} | "
                f"Quality={quality} | Score={candidate.score:g} | "
                f"Confidence={candidate.confidence:g}% | "
                f"Weighted={candidate.weighted_score:g} | Edge={candidate.edge:g}"
            )
        self._print("=" * 60)

    def signal_counts(self, counts: Mapping[str, int]) -> None:
        self._print(
            "Signal counts this cycle:",
            f"HIGH PRIORITY: {counts.get('HIGH PRIORITY', 0)}",
            f"SETUP       : {counts.get('SETUP', 0)}",
            f"WATCH       : {counts.get('WATCH', 0)}",
            f"WAIT        : {counts.get('WAIT', 0)}",
            f"NO TRADE    : {counts.get('NO TRADE', 0)}",
        )

    def checking_trade(self, symbol: str) -> None:
        self._print(f"Checking trade: {symbol}")

    def trade_snapshot(self, symbol: str, direction: str, price: float, sl: float, tp: float) -> None:
        if self.allows("NORMAL"):
            self._print(
                f"{symbol} | {direction} | Price={price:.2f} | SL={sl:.2f} | TP={tp:.2f}"
            )

    def trade_result(self, symbol: str, result: str) -> None:
        self._print(f"{symbol} -> {result}")

    def api_retry(self, symbol: str, timeframe: str, attempt: int, total: int, error: Exception) -> None:
        self.timestamped(
            f"API error {symbol} {timeframe} ({attempt}/{total}): {error}",
            minimum="DEBUG",
        )

    def cache_used(self, symbol: str, timeframe: str) -> None:
        self.timestamped(f"Using cached OHLCV for {symbol} {timeframe}", minimum="DEBUG")

    def engine_timings(self, symbol: str, timings: Mapping[str, float]) -> None:
        if not self.allows("DEBUG"):
            return
        line = (
            f"{symbol} engine timings | trend={timings.get('trend', 0):.4f}s | "
            f"structure={timings.get('structure', 0):.4f}s | "
            f"momentum={timings.get('momentum', 0):.4f}s | "
            f"risk={timings.get('risk', 0):.4f}s | "
            f"decision={timings.get('decision', 0):.4f}s"
        )
        self.timestamped(line, minimum="DEBUG")

    def csv_read(self, path: str) -> None:
        self.timestamped(f"Read {path}", minimum="DEBUG")

    def csv_write(self, path: str) -> None:
        self.timestamped(f"Wrote {path}", minimum="DEBUG")

    def schema_migrated(self, path: str) -> None:
        self.timestamped(f"Migrated CSV schema for {path}", minimum="DEBUG")

    def finished(self) -> None:
        self._print("Finished.")
