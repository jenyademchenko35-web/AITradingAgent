"""Build local OHLCV cache for blocked trade simulation."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from config import TIMEFRAME

try:
    import ccxt
except ModuleNotFoundError:  # pragma: no cover - runtime environment dependent
    ccxt = None


BASE_DIR = Path(__file__).resolve().parent
CANDIDATES_FILE = BASE_DIR / "blocked_trade_candidates.csv"
CACHE_DIR = BASE_DIR / "ohlcv_cache"

LOOKBACK_HOURS = 120
LOOKAHEAD_HOURS = 48
FETCH_LIMIT = 1000


def parse_time(value: str) -> Optional[datetime]:
    """Parse ISO timestamps into UTC datetimes."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_candidates() -> List[Dict[str, str]]:
    """Read blocked trade candidates from disk."""
    if not CANDIDATES_FILE.exists() or CANDIDATES_FILE.stat().st_size == 0:
        return []
    with CANDIDATES_FILE.open("r", newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    return [row for row in rows if row and any(row.values())]


def build_exchange():
    """Create ccxt Bybit client."""
    if ccxt is None:
        raise RuntimeError("ccxt не установлен в текущем окружении.")
    return ccxt.bybit(
        {
            "enableRateLimit": True,
            "timeout": 30000,
            "options": {"defaultType": "spot"},
        }
    )


def symbol_cache_path(symbol: str) -> Path:
    """Return cache path for a symbol."""
    return CACHE_DIR / f"{symbol.replace('/', '_')}_{TIMEFRAME}.csv"


def fetch_ohlcv_window(exchange, symbol: str, since_dt: datetime) -> List[List[float]]:
    """Fetch OHLCV window around the candidate time."""
    since_ms = int((since_dt - timedelta(hours=LOOKBACK_HOURS)).timestamp() * 1000)
    return exchange.fetch_ohlcv(
        symbol,
        timeframe=TIMEFRAME,
        since=since_ms,
        limit=FETCH_LIMIT,
    )


def write_cache(path: Path, rows: List[List[float]]) -> None:
    """Write OHLCV rows to cache."""
    CACHE_DIR.mkdir(exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for ts, open_, high, low, close, volume in rows:
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
            writer.writerow([dt, open_, high, low, close, volume])


def main() -> None:
    """Build OHLCV cache files for symbols from blocked candidates."""
    candidates = read_candidates()
    if not candidates:
        print("OHLCV Cache Builder")
        print("Кандидаты не найдены. Сначала запусти blocked_trade_simulator.py.")
        return

    by_symbol: Dict[str, datetime] = {}
    for row in candidates:
        symbol = row.get("symbol", "")
        ts = parse_time(row.get("timestamp", ""))
        if not symbol or ts is None:
            continue
        if symbol not in by_symbol or ts < by_symbol[symbol]:
            by_symbol[symbol] = ts

    exchange = build_exchange()
    built = 0
    for symbol, ts in sorted(by_symbol.items()):
        rows = fetch_ohlcv_window(exchange, symbol, ts)
        if not rows:
            print(f"{symbol}: OHLCV не получен")
            continue
        write_cache(symbol_cache_path(symbol), rows)
        built += 1
        print(f"{symbol}: сохранено {len(rows)} свечей -> {symbol_cache_path(symbol).name}")

    print("OHLCV Cache Builder")
    print(f"Symbols processed : {len(by_symbol)}")
    print(f"Cache files built : {built}")
    print(f"Cache directory   : {CACHE_DIR}")


if __name__ == "__main__":
    main()
