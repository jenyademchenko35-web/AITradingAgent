import csv
from pathlib import Path

TRADES_FILE = Path("trades.csv")


def get_statistics():
    if not TRADES_FILE.exists():
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "open": 0,
            "win_rate": 0.0,
        }

    wins = 0
    losses = 0
    open_trades = 0
    by_symbol = {}
    long_wins = 0
    long_losses = 0
    short_wins = 0
    short_losses = 0

    with open(TRADES_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            status = row["status"]
            symbol = row["symbol"]
            direction = row["direction"]

            if symbol not in by_symbol:
                by_symbol[symbol] = {
                    "wins": 0,
                    "losses": 0,
                }

            if status == "WIN":
                by_symbol[symbol]["wins"] += 1
                wins += 1

                if direction == "LONG":
                    long_wins += 1
                else:
                    short_wins += 1

            elif status == "LOSS":
                by_symbol[symbol]["losses"] += 1
                losses += 1

                if direction == "LONG":
                    long_losses += 1
                else:
                    short_losses += 1
            elif status == "OPEN":
                open_trades += 1

    total = wins + losses + open_trades
    closed = wins + losses
    win_rate = (wins / closed * 100) if closed else 0.0

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "open": open_trades,
        "win_rate": round(win_rate, 1),
        "by_symbol": by_symbol,
        "long_wins": long_wins,
        "long_losses": long_losses,
        "short_wins": short_wins,
        "short_losses": short_losses,
    }


def format_statistics():
    stats = get_statistics()
    symbol_lines = []

    for symbol, data in stats["by_symbol"].items():
        total = data["wins"] + data["losses"]

        if total:
            win_rate = data["wins"] / total * 100
        else:
            win_rate = 0

        symbol_lines.append(
            f"{symbol}: {data['wins']}W / {data['losses']}L ({win_rate:.0f}%)"
        )

    symbol_text = "\n".join(symbol_lines)

    return (
        "📊 AI Trading Agent\n\n"
        f"📈 Всего сделок: {stats['total']}\n"
        f"🟢 Побед: {stats['wins']}\n"
        f"🔴 Поражений: {stats['losses']}\n"
        f"🕒 Открытых: {stats['open']}\n"
        f"🏆 Win Rate: {stats['win_rate']}%\n\n"
        f"📈 LONG: {stats['long_wins']}W / {stats['long_losses']}L\n"
        f"📉 SHORT: {stats['short_wins']}W / {stats['short_losses']}L\n\n"
        f"📊 По монетам:\n{symbol_text}"
    )
