import pandas as pd


def calculate_trade_stats(df: pd.DataFrame):
    wins = df[df["Result"] == "WIN"]
    losses = df[df["Result"] == "LOSS"]

    total = len(df)
    win_count = len(wins)
    loss_count = len(losses)

    win_rate = (win_count / total * 100) if total else 0

    avg_win = wins["PnL"].mean() if win_count else 0
    avg_loss = losses["PnL"].mean() if loss_count else 0

    expectancy = (
        (win_rate / 100) * avg_win +
        ((100 - win_rate) / 100) * avg_loss
    )

    avg_duration = df["DurationHours"].mean() if total else 0

    return {
        "trades": total,
        "wins": win_count,
        "losses": loss_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "avg_duration": avg_duration,
    }
