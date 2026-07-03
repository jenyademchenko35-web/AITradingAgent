import pandas as pd
import matplotlib.pyplot as plt
from analytics import calculate_trade_stats

START_BALANCE = 1000.0

df = pd.read_csv("backtest_trades.csv")

stats = calculate_trade_stats(df)

balance = START_BALANCE
equity = [balance]

RISK_PER_TRADE = 0.01      # 1% риска на сделку
RR = 2.0                   # Risk/Reward = 1:2

for pnl in df["PnL"]:

    if pnl > 0:
        balance *= 1 + (RISK_PER_TRADE * RR)
    else:
        balance *= 1 - RISK_PER_TRADE

    equity.append(balance)

print(f"Initial balance : ${START_BALANCE:.2f}")
print(f"Final balance   : ${balance:.2f}")
print(f"Net Profit      : ${balance - START_BALANCE:.2f}")

peak = START_BALANCE
max_drawdown = 0.0
max_drawdown_amount = 0.0

for value in equity:
    peak = max(peak, value)

    drawdown_amount = peak - value
    drawdown_percent = (
        drawdown_amount / peak * 100
        if peak > 0
        else 0
    )

    if drawdown_percent > max_drawdown:
        max_drawdown = drawdown_percent
        max_drawdown_amount = drawdown_amount

print(f"Max Drawdown    : {max_drawdown:.2f}%")
print(f"Drawdown Amount : ${max_drawdown_amount:.2f}")
print("\n========== BACKTEST REPORT ==========")
print(f"Trades         : {stats['trades']}")
print(f"Wins           : {stats['wins']}")
print(f"Losses         : {stats['losses']}")
print(f"Win Rate       : {stats['win_rate']:.2f}%")
print(f"Average Win    : ${stats['avg_win']:.2f}")
print(f"Average Loss   : ${stats['avg_loss']:.2f}")
print(f"Expectancy     : ${stats['expectancy']:.2f}")
print(f"Max Drawdown   : {max_drawdown:.2f}%")
print(f"Final Balance  : ${balance:.2f}")
return_pct = (balance / START_BALANCE - 1) * 100
print(f"Return         : {return_pct:.2f}%")
print("====================================")

plt.figure(figsize=(12,6))
plt.plot(equity)

plt.title("Equity Curve")
plt.xlabel("Closed Trades")
plt.ylabel("Balance ($)")
plt.grid(True)

plt.show()
