import ccxt
import pandas as pd

from multi_timeframe_agent_v3 import (
    analyze_market,
    build_market_snapshot,
)

EXCHANGE = ccxt.bybit({
    "enableRateLimit": True,
    "timeout": 30000,
    "options": {
        "defaultType": "spot",
    },
})

SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"
LIMIT = 2000


def load_history():
    data = EXCHANGE.fetch_ohlcv(
        SYMBOL,
        timeframe=TIMEFRAME,
        limit=LIMIT,
    )

    df = pd.DataFrame(
        data,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ],
    )

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def run_backtest(df):
    print("\nRunning candle-by-candle simulation...")

    virtual_trade = None

    stats = {
        "trades": 0,
        "wins": 0,
        "losses": 0,
    }

    start_bar = 24 * 30

    for i in range(start_bar, len(df)):
        trade_closed = False

        history = df.iloc[: i + 1].copy()
        if i + 1 >= len(df):
            break

        next_open = df.iloc[i + 1]["open"]

        history_1h = history

        history_4h = (
            history
            .set_index("timestamp")
            .resample("4h")
            .agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            })
            .dropna()
            .reset_index()
        )

        history_1d = (
            history
            .set_index("timestamp")
            .resample("1D")
            .agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            })
            .dropna()
            .reset_index()
        )

        # Wait until enough candles exist for all indicators.
        if (
            len(history_1h) < 60
            or len(history_4h) < 60
            or len(history_1d) < 20
        ):
            continue

        market = build_market_snapshot(
            symbol=SYMBOL,
            tf1h_df=history_1h,
            tf4h_df=history_4h,
            tf1d_df=history_1d,
        )


        decision, trend, structure, momentum, risk = analyze_market(
            market=market,
            symbol=SYMBOL,
        )

        current_close = history.iloc[-1]["close"]
        current_time = history.iloc[-1]["timestamp"]

        print(
            f"[{current_time}] Close={current_close:.2f}",
            end="\r",
        )

        # Check if virtual trade hits SL/TP on this bar
        if (
            virtual_trade is not None
            and i > virtual_trade["entry_bar"]
        ):
            high = history.iloc[-1]["high"]
            low = history.iloc[-1]["low"]

            if virtual_trade["direction"] == "LONG":
                if low <= virtual_trade["sl"]:
                    stats["losses"] += 1
                    print(f"\nLOSS LONG @ {current_time}")
                    virtual_trade = None
                    trade_closed = True
                elif high >= virtual_trade["tp"]:
                    stats["wins"] += 1
                    print(f"\nWIN LONG @ {current_time}")
                    virtual_trade = None
                    trade_closed = True
            else:
                if high >= virtual_trade["sl"]:
                    stats["losses"] += 1
                    print(f"\nLOSS SHORT @ {current_time}")
                    virtual_trade = None
                    trade_closed = True
                elif low <= virtual_trade["tp"]:
                    stats["wins"] += 1
                    print(f"\nWIN SHORT @ {current_time}")
                    virtual_trade = None
                    trade_closed = True

        if virtual_trade is None and not trade_closed:
            if (
                decision.signal in ("SETUP", "HIGH PRIORITY")
                and decision.direction != "NEUTRAL"
            ):
                atr = market.tf1h.atr

                if decision.direction == "LONG":
                    stop_loss = next_open - atr
                    take_profit = next_open + atr * 2
                else:
                    stop_loss = next_open + atr
                    take_profit = next_open - atr * 2

                virtual_trade = {
                    "direction": decision.direction,
                    "entry": next_open,
                    "sl": stop_loss,
                    "tp": take_profit,
                    "time": current_time,
                    "entry_bar": i + 1,
                }

                stats["trades"] += 1

                print(
                    f"\nOPEN {decision.direction} @ "
                    f"{next_open:.2f}"
                )

        if i % 50 == 0 or i == len(df) - 1:
            print(
                f"Processed {i + 1}/{len(df)} bars | "
                f"Current candle: {history.iloc[-1]['timestamp']}"
            )

    print()
    print("========== BACKTEST COMPLETED ==========")
    print("========== RESULTS ==========")
    print(f"Trades : {stats['trades']}")
    print(f"Wins   : {stats['wins']}")
    print(f"Losses : {stats['losses']}")

    closed_trades = stats["wins"] + stats["losses"]

    win_rate = (
        stats["wins"] / closed_trades * 100
        if closed_trades > 0
        else 0
    )

    print(f"Closed : {closed_trades}")
    print(f"Open   : {stats['trades'] - closed_trades}")
    print(f"WinRate: {win_rate:.2f}%")


if __name__ == "__main__":
    df = load_history()

    print("========== BACKTEST ==========")
    print(f"Symbol : {SYMBOL}")
    print(f"Bars   : {len(df)}")
    print(f"From   : {df.iloc[0]['timestamp']}")
    print(f"To     : {df.iloc[-1]['timestamp']}")
    print(df.tail())
    run_backtest(df)
