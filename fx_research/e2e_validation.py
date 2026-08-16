"""Deterministic, local-only E2E validation for isolated FX shadow research.

It uses only fixed fixture candles and a temporary directory.  It never reads
or writes crypto state, contacts a provider, or creates a persistent artifact.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .checkpoint import build_checkpoint
from .config import FXResearchSettings
from .features import build_feature_snapshot, evaluate_strategy
from .provider import FXCandle
from .runtime import FXOutcomeStore, FXShadowBook

UTC = timezone.utc


def _candle(symbol: str, at: datetime, *, high: float, low: float, close: float) -> FXCandle:
    """Normalise the canonical fixture shape through the provider boundary."""
    return FXCandle.from_mapping({
        "symbol": symbol, "timeframe": "1h", "candle_open_at": at.isoformat(),
        "open": close, "high": high, "low": low,
        "close": close, "volume": None, "source": "deterministic_fixture",
        "fetched_at": (at + timedelta(seconds=1)).isoformat(),
    })


def _settings(root: Path) -> FXResearchSettings:
    return FXResearchSettings(
        enabled=False, open_book_path=root / "fx_shadow_open.json",
        history_path=root / "fx_shadow_history.csv", pending_closes_path=root / "fx_pending_closes.json",
        database_path=root / "fx_research.db",
    )


def _snapshot(symbol: str, at: datetime) -> tuple[FXCandle, dict[str, Any]]:
    base = 1.10 if symbol == "EUR/USD" else 1.30
    history = [
        _candle(symbol, at - timedelta(hours=15 - index), high=base + index * 0.0015,
                low=base + index * 0.001 - 0.0005, close=base + index * 0.001)
        for index in range(16)
    ]
    current = history[-1]
    snapshot = build_feature_snapshot(current, history)
    # The fixture explicitly declares its strategy-ready evidence; the source
    # candle list remains immutable and no future candle is consulted.
    snapshot = {**snapshot, "atr": 0.01, "rsi": 50.0, "trend": "UP", "momentum": "UP"}
    return current, snapshot


def _open(book: FXShadowBook, symbol: str, at: datetime) -> dict[str, Any]:
    _, snapshot = _snapshot(symbol, at)
    decision = evaluate_strategy("FX_TREND_CONFIRM", snapshot)
    if not decision["accepted"]:
        raise AssertionError("deterministic fixture must create an FX baseline decision")
    trade = book.open(strategy_id="FX_TREND_CONFIRM", snapshot=snapshot, side=str(decision["direction"]))
    if not trade:
        raise AssertionError("fixture shadow trade was unexpectedly blocked")
    return trade


class _FailingStore:
    """Simulates a crash-window persistence failure without modifying runtime code."""

    def persist(self, trade: Mapping[str, Any]) -> str:
        raise sqlite3.OperationalError("fixture persistence unavailable")


def _outcome_count(path: Path, trade_id: str) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM fx_shadow_trade_outcomes WHERE shadow_trade_id=?", (trade_id,)
        ).fetchone()[0])


def run_validation() -> dict[str, Any]:
    """Run winning, losing, ambiguous and restart-recovery fixture scenarios."""
    with tempfile.TemporaryDirectory(prefix="fx-shadow-e2e-") as directory:
        root = Path(directory)
        settings = _settings(root)
        book, store = FXShadowBook(settings), FXOutcomeStore(settings.database_path)
        start = datetime(2026, 8, 17, 10, tzinfo=UTC)

        eur_winner = _open(book, "EUR/USD", start)
        # Same open book after a "restart" receives a non-closing candle first.
        FXShadowBook(settings).close_from_candle(
            _candle("EUR/USD", start + timedelta(hours=1), high=1.117, low=1.112, close=1.115), store,
        )
        reloaded_open = FXShadowBook(settings).load()[0]
        restart_open_ok = (
            reloaded_open["shadow_trade_id"] == eur_winner["shadow_trade_id"]
            and reloaded_open["holding_candles"] == 1
            and reloaded_open["mfe_r"] > 0 and reloaded_open["mae_r"] < 0
        )
        eur_closed = FXShadowBook(settings).close_from_candle(
            _candle("EUR/USD", start + timedelta(hours=2), high=1.140, low=1.114, close=1.128), store,
        )[0]

        _open(book, "GBP/USD", start + timedelta(hours=3))
        gbp_closed = book.close_from_candle(
            _candle("GBP/USD", start + timedelta(hours=4), high=1.316, low=1.303, close=1.306), store,
        )[0]

        _open(book, "EUR/USD", start + timedelta(hours=5))
        ambiguous_closed = book.close_from_candle(
            _candle("EUR/USD", start + timedelta(hours=6), high=1.140, low=1.100, close=1.120), store,
        )[0]

        pending_trade = _open(book, "GBP/USD", start + timedelta(hours=7))
        book.close_from_candle(
            _candle("GBP/USD", start + timedelta(hours=8), high=1.330, low=1.300, close=1.303), _FailingStore(),
        )[0]
        # Simulate process restart: ledger+outbox exist, canonical persistence did not.
        restarted_book = FXShadowBook(settings)
        first = restarted_book.reconcile(store)
        second = FXShadowBook(settings).reconcile(store)
        recovery_ok = (
            first["recovered"] == 1 and second["recovered"] == 0
            and _outcome_count(settings.database_path, str(pending_trade["shadow_trade_id"])) == 1
            and not FXShadowBook(settings).load()
        )

        checkpoint = build_checkpoint(settings.database_path)
        with sqlite3.connect(settings.database_path) as connection:
            persisted = [dict(zip(("asset_class", "shadow_trade_id", "feature_snapshot_id", "signal_id", "decision_id", "join_status"), row))
                         for row in connection.execute(
                             "SELECT asset_class, shadow_trade_id, feature_snapshot_id, signal_id, decision_id, join_status FROM fx_shadow_trade_outcomes"
                         )]
        crypto_artifacts = (root / "research.db", root / "research_lab_v2_shadow_open.json", root / "research_lab_shadow_history.csv")
        attribution_ok = all(
            row["asset_class"] == "FX" and row["feature_snapshot_id"] and row["signal_id"] and row["decision_id"]
            for row in persisted
        )
        checkpoint_ok = checkpoint["integrity"]["eligible"] == 3 and checkpoint["integrity"]["unresolved"] == 1
        return {
            "overall": all((
                eur_closed["exit_reason"] == "TAKE_PROFIT", eur_closed["pnl_r"] == 2.0,
                gbp_closed["exit_reason"] == "STOP_LOSS", gbp_closed["pnl_r"] == -1.0,
                ambiguous_closed["exit_reason"] == "AMBIGUOUS_INTRABAR", ambiguous_closed["pnl_r"] is None,
                restart_open_ok, recovery_ok, attribution_ok, checkpoint_ok,
                all(not path.exists() for path in crypto_artifacts),
            )),
            "EUR/USD": "PASS" if eur_closed["pnl_r"] == 2.0 else "FAIL",
            "GBP/USD": "PASS" if gbp_closed["pnl_r"] == -1.0 else "FAIL",
            "ambiguous_intrabar": "PASS" if ambiguous_closed["pnl_r"] is None else "FAIL",
            "restart_recovery": "PASS" if recovery_ok else "FAIL",
            "idempotency": "PASS" if recovery_ok else "FAIL",
            "attribution": "PASS" if attribution_ok else "FAIL",
            "checkpoint_integrity": "PASS" if checkpoint_ok else "FAIL",
            "crypto_isolation": "PASS" if all(not path.exists() for path in crypto_artifacts) else "FAIL",
            "checkpoint": checkpoint,
        }


def main() -> int:
    result = run_validation()
    print("FX E2E VALIDATION")
    for label in ("EUR/USD", "GBP/USD", "restart_recovery", "idempotency", "crypto_isolation", "checkpoint_integrity"):
        print(f"{label.replace('_', ' ').title()}: {result[label]}")
    print("Overall: " + ("PASS" if result["overall"] else "FAIL"))
    return 0 if result["overall"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
