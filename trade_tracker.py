import csv
from pathlib import Path
from datetime import datetime

TRADES_FILE = Path("trades.csv")
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
            trades.append(row)
    with TRADES_FILE.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(trades)

    print(f"Trade closed: {symbol} -> {result}")