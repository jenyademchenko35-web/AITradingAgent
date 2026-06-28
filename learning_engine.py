import csv
from pathlib import Path

TRADES_FILE = Path("trades.csv")

from datetime import datetime

HISTORY_FILE = Path("learning_history.csv")

def analyze_trades():
    if not TRADES_FILE.exists():
        print("trades.csv not found")
        return

    total = 0
    wins = 0
    losses = 0
    total_pnl = 0.0
    profit = 0.0
    loss = 0.0
    by_symbol = {}

    with TRADES_FILE.open(newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            if row.get("status") not in ("WIN", "LOSS"):
                continue

            total += 1
            symbol = row["symbol"]
            pnl = float(row.get("pnl") or 0)

            if symbol not in by_symbol:
                by_symbol[symbol] = {"wins": 0, "losses": 0, "pnl": 0.0}

            by_symbol[symbol]["pnl"] += pnl

            if row["status"] == "WIN":
                wins += 1
                profit += max(pnl, 0)
                by_symbol[symbol]["wins"] += 1
            else:
                losses += 1
                loss += abs(min(pnl, 0))
                by_symbol[symbol]["losses"] += 1

            total_pnl += pnl

    profit_factor = profit / loss if loss else float("inf")
    avg_pnl = total_pnl / total if total else 0
    win_rate = wins / total * 100 if total else 0

    header = [
        "date",
        "closed_trades",
        "wins",
        "losses",
        "win_rate",
        "total_pnl",
        "average_pnl",
        "profit_factor",
    ]

    write_header = not HISTORY_FILE.exists()

    with HISTORY_FILE.open("a", newline="") as f:
        writer = csv.writer(f)

        if write_header:
            writer.writerow(header)

        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            total,
            wins,
            losses,
            round(win_rate, 1),
            round(total_pnl, 2),
            round(avg_pnl, 2),
            round(profit_factor, 2),
        ])

    print("===== LEARNING REPORT =====")
    print(f"Closed trades : {total}")
    print(f"Wins          : {wins}")
    print(f"Losses        : {losses}")
    print(f"Total PnL     : {total_pnl:.2f}")
    print(f"Average PnL   : {avg_pnl:.2f}")
    print(f"Profit Factor : {profit_factor:.2f}")
    print()

    for symbol, data in by_symbol.items():
        closed = data["wins"] + data["losses"]
        wr = data["wins"] / closed * 100 if closed else 0
        print(f"{symbol}: WR={wr:.1f}% | PnL={data['pnl']:.2f}")

    print()
    print("===== RECOMMENDATIONS =====")

    for symbol, data in by_symbol.items():
        closed = data["wins"] + data["losses"]

        if closed < 10:
            print(f"{symbol}: недостаточно данных ({closed} сделок)")
            continue

        wr = data["wins"] / closed * 100

        if wr < 40:
            print(f"{symbol}: WR={wr:.1f}%")
            print("  → Reduce Trend weight by 5%")
            print("  → Reduce Structure weight by 5%")
        elif wr > 60:
            print(f"{symbol}: WR={wr:.1f}%")
            print("  → Increase Trend weight by 5%")
            print("  → Keep current strategy")
        else:
            print(f"{symbol}: WR={wr:.1f}%")
            print("  → Keep weights unchanged")


if __name__ == "__main__":
    analyze_trades()