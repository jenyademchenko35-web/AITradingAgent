"""Build local 1h OHLCV cache for live AITradingAgent symbols.

The utility is read-only with respect to strategy code: it does not modify
config.py, DecisionEngine, strategy weights, or the live agent. It only writes
CSV cache files under ohlcv_cache/ in the format consumed by
min_edge_outcome_analysis.py.
"""

from __future__ import annotations

import ast
import csv
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence


try:
    import ccxt
except ModuleNotFoundError:  # pragma: no cover - runtime environment dependent
    ccxt = None


BASE_DIR = Path(__file__).resolve().parent
AGENT_FILE = BASE_DIR / "multi_timeframe_agent_v3.py"
CACHE_DIR = BASE_DIR / "ohlcv_cache"

TIMEFRAME = "1h"
MIN_LIMIT = 2000
FETCH_LIMIT = 1000
REQUEST_PAUSE_SECONDS = 0.25

FALLBACK_SYMBOLS: Sequence[str] = (
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "XRP/USDT",
    "LINK/USDT",
    "ADA/USDT",
    "AVAX/USDT",
    "DOGE/USDT",
)


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def load_live_symbols() -> List[str]:
    """Read SYMBOLS from multi_timeframe_agent_v3.py without importing it."""
    if not AGENT_FILE.exists():
        return list(FALLBACK_SYMBOLS)

    try:
        tree = ast.parse(AGENT_FILE.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return list(FALLBACK_SYMBOLS)

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "SYMBOLS" for target in node.targets):
            continue
        try:
            symbols = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            return list(FALLBACK_SYMBOLS)
        if isinstance(symbols, list) and all(isinstance(item, str) for item in symbols):
            return symbols
    return list(FALLBACK_SYMBOLS)


def build_exchange() -> Any:
    """Create a Bybit spot exchange client."""
    if ccxt is None:
        raise RuntimeError("ccxt is not installed in the current environment.")
    return ccxt.bybit(
        {
            "enableRateLimit": True,
            "timeout": 30000,
            "options": {"defaultType": "spot"},
        }
    )


def cache_path(symbol: str) -> Path:
    """Return cache path for a symbol."""
    return CACHE_DIR / f"{symbol.replace('/', '_')}_{TIMEFRAME}.csv"


def normalize_rows(rows: Sequence[Sequence[Any]]) -> List[List[Any]]:
    """Sort and deduplicate OHLCV rows by timestamp."""
    by_timestamp: Dict[int, Sequence[Any]] = {}
    for row in rows:
        if len(row) < 6:
            continue
        try:
            timestamp = int(row[0])
        except (TypeError, ValueError):
            continue
        by_timestamp[timestamp] = row
    return [list(by_timestamp[key]) for key in sorted(by_timestamp)]


def fetch_symbol_ohlcv(exchange: Any, symbol: str) -> List[List[Any]]:
    """Fetch at least MIN_LIMIT hourly OHLCV rows when the API allows it."""
    since = datetime.now(timezone.utc) - timedelta(hours=MIN_LIMIT + 24)
    since_ms = int(since.timestamp() * 1000)
    rows: List[List[Any]] = []
    last_timestamp = 0

    while len(rows) < MIN_LIMIT:
        batch = exchange.fetch_ohlcv(
            symbol,
            timeframe=TIMEFRAME,
            since=since_ms,
            limit=FETCH_LIMIT,
        )
        if not batch:
            break

        rows.extend(batch)
        normalized = normalize_rows(rows)
        rows = normalized

        newest_timestamp = int(rows[-1][0])
        if newest_timestamp <= last_timestamp:
            break
        last_timestamp = newest_timestamp
        since_ms = newest_timestamp + 1

        time.sleep(REQUEST_PAUSE_SECONDS)

    return normalize_rows(rows)[-MIN_LIMIT:]


def write_cache(symbol: str, rows: Sequence[Sequence[Any]]) -> None:
    """Write OHLCV rows using the existing cache CSV format."""
    CACHE_DIR.mkdir(exist_ok=True)
    with cache_path(symbol).open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for ts, open_, high, low, close, volume in rows:
            timestamp = datetime.fromtimestamp(float(ts) / 1000, tz=timezone.utc).isoformat()
            writer.writerow([timestamp, open_, high, low, close, volume])


def build_cache() -> Dict[str, Any]:
    """Build cache for all live symbols and return a run summary."""
    symbols = load_live_symbols()
    summary: Dict[str, Any] = {
        "generated_at": utc_now(),
        "timeframe": TIMEFRAME,
        "min_limit": MIN_LIMIT,
        "symbols": symbols,
        "built": {},
        "warnings": {},
    }

    try:
        exchange = build_exchange()
    except Exception as exc:  # noqa: BLE001 - warning-only CLI utility
        warning = f"Exchange unavailable: {exc}"
        summary["warnings"]["exchange"] = warning
        return summary

    for symbol in symbols:
        try:
            rows = fetch_symbol_ohlcv(exchange, symbol)
            if not rows:
                summary["warnings"][symbol] = "No OHLCV rows returned."
                continue
            write_cache(symbol, rows)
            summary["built"][symbol] = {
                "rows": len(rows),
                "file": str(cache_path(symbol)),
                "from": datetime.fromtimestamp(float(rows[0][0]) / 1000, tz=timezone.utc).isoformat(),
                "to": datetime.fromtimestamp(float(rows[-1][0]) / 1000, tz=timezone.utc).isoformat(),
            }
            if len(rows) < MIN_LIMIT:
                summary["warnings"][symbol] = (
                    f"Only {len(rows)} rows fetched; requested at least {MIN_LIMIT}."
                )
        except Exception as exc:  # noqa: BLE001 - keep building other symbols
            summary["warnings"][symbol] = str(exc)
    return summary


def print_summary(summary: Dict[str, Any]) -> None:
    """Print a compact run summary."""
    print("OHLCV Cache Builder")
    print(f"Timeframe       : {summary.get('timeframe', TIMEFRAME)}")
    print(f"Requested limit : {summary.get('min_limit', MIN_LIMIT)}")
    print(f"Symbols         : {len(summary.get('symbols', []))}")
    print(f"Built           : {len(summary.get('built', {}))}")
    print(f"Warnings        : {len(summary.get('warnings', {}))}")
    for symbol, payload in summary.get("built", {}).items():
        print(f"{symbol}: {payload.get('rows', 0)} rows -> {Path(payload.get('file', '')).name}")
    for symbol, warning in summary.get("warnings", {}).items():
        print(f"WARNING {symbol}: {warning}")


def main() -> None:
    """Build local OHLCV cache files."""
    summary = build_cache()
    print_summary(summary)


if __name__ == "__main__":
    main()
