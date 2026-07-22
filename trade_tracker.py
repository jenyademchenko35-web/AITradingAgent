import csv
import logging
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
TRADES_FILE = BASE_DIR / "trades.csv"
FIELDS = [
    "symbol",
    "direction",
    "entry",
    "stop_loss",
    "take_profit",
    "status",
    "result",
    "opened_at",
    "closed_at",
    "exit_price",
    "pnl",
]
LOGGER = logging.getLogger(__name__)


def _research_diagnostics(trade, *, snapshot=False):
    """Best-effort diagnostics; failures must never affect trade persistence."""
    try:
        from research_data_quality import build_decision_snapshot, validate_research_trade
        missing = validate_research_trade(trade)
        if missing:
            LOGGER.warning("trade research fields unavailable: %s", ", ".join(missing))
        if snapshot:
            build_decision_snapshot(symbol=trade.get("symbol"), opened_at=trade.get("opened_at"), extra=trade)
    except Exception as error:  # research instrumentation is deliberately fail-open
        LOGGER.warning("research diagnostics failed: %s", error)

def ensure_file():
    if not TRADES_FILE.exists():
        with TRADES_FILE.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()

def open_trade(symbol, direction, entry, stop_loss, take_profit):
    ensure_file()
    now = datetime.utcnow().isoformat()
    trade = {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "status": "OPEN",
        "result": "",
        "opened_at": now,
        "closed_at": "",
        "exit_price": "",
        "pnl": "",
    }
    with TRADES_FILE.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writerow(trade)
    _research_diagnostics(trade, snapshot=True)

def get_open_trades():
    ensure_file()
    trades = []
    with TRADES_FILE.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["status"] == "OPEN":
                trades.append(row)
    return trades

def close_trade(symbol, result, exit_price=None, pnl=None):
    ensure_file()
    trades = []
    now = datetime.utcnow().isoformat()
    with TRADES_FILE.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["symbol"] == symbol and row["status"] == "OPEN":
                row["status"] = result
                row["result"] = result
                row["closed_at"] = now
                row["exit_price"] = exit_price if exit_price is not None else ""
                row["pnl"] = pnl if pnl is not None else ""
                _research_diagnostics(row)
            trades.append(row)
    with TRADES_FILE.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(trades)

    print(f"Trade closed: {symbol} -> {result}")
