import argparse
import csv
import os
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any, Dict, List

import ccxt
import pandas as pd


SIGNALS_FILE = "signals.csv"
RESULTS_FILE = "backtest_results.csv"
CHECK_HOURS = [24, 48, 72]
DEFAULT_MIN_CONFIDENCE = 60

EXCHANGE = ccxt.bybit(
    {
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    }
)


def read_signals(
    filename: str = SIGNALS_FILE,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> List[Dict[str, Any]]:
    if not os.path.exists(filename):
        raise FileNotFoundError(f"{filename} not found")

    df = pd.read_csv(filename)
    if df.empty:
        return []

    required_columns = {"timestamp", "symbol", "price"}
    missing_columns = required_columns - set(df.columns)
    if missing_columns:
        raise ValueError(f"Missing columns in {filename}: {', '.join(sorted(missing_columns))}")

    records = []
    for record in df.to_dict("records"):
        raw_signal = str(record.get("raw_signal") or record.get("signal") or "").upper()
        raw_confidence = parse_optional_float(
            record.get("raw_confidence") or record.get("confidence")
        )

        if raw_signal not in {"LONG", "SHORT"}:
            continue
        if raw_confidence is None or raw_confidence < min_confidence:
            continue

        entry = parse_optional_float(record.get("entry"))
        stop_loss = parse_optional_float(record.get("stop_loss"))
        take_profit = parse_optional_float(record.get("take_profit"))

        records.append(
            {
                "timestamp": parse_timestamp(record["timestamp"]),
                "symbol": str(record["symbol"]),
                "entry_price": entry if entry is not None else float(record["price"]),
                "signal": raw_signal,
                "raw_confidence": raw_confidence,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "rr": parse_optional_float(record.get("rr")),
            }
        )

    return records


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_optional_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None

    text = str(value).strip()
    if not text or text.upper() == "N/A":
        return None

    return float(text.replace("%", "").replace(",", ""))


def get_ohlcv_until(symbol: str, start_time: datetime, end_time: datetime) -> List[List[float]]:
    since = int(start_time.timestamp() * 1000)
    hours = max(1, int((end_time - start_time).total_seconds() // 3600) + 2)
    ohlcv = EXCHANGE.fetch_ohlcv(symbol, timeframe="1h", since=since, limit=hours)
    end_ms = int(end_time.timestamp() * 1000)
    candles = [candle for candle in ohlcv if candle[0] <= end_ms]

    if not candles:
        raise RuntimeError(f"No OHLCV data for {symbol} from {start_time.isoformat()}")

    return candles


def calculate_profit_percent(signal: str, entry_price: float, future_price: float) -> float:
    if signal == "LONG":
        return (future_price - entry_price) / entry_price * 100
    if signal == "SHORT":
        return (entry_price - future_price) / entry_price * 100
    raise ValueError(f"Unsupported signal: {signal}")


def evaluate_trade(
    signal: str,
    entry_price: float,
    stop_loss: float | None,
    take_profit: float | None,
    candles: List[List[float]],
) -> Dict[str, Any]:
    exit_price = float(candles[-1][4])
    exit_reason = "TIME_EXIT"
    exit_time = datetime.fromtimestamp(candles[-1][0] / 1000, tz=timezone.utc)

    highs = [float(candle[2]) for candle in candles]
    lows = [float(candle[3]) for candle in candles]

    if signal == "LONG":
        max_profit = (max(highs) - entry_price) / entry_price * 100
        max_drawdown = (min(lows) - entry_price) / entry_price * 100
    else:
        max_profit = (entry_price - min(lows)) / entry_price * 100
        max_drawdown = (entry_price - max(highs)) / entry_price * 100

    for candle in candles:
        candle_time = datetime.fromtimestamp(candle[0] / 1000, tz=timezone.utc)
        high = float(candle[2])
        low = float(candle[3])

        if signal == "LONG":
            hit_stop = stop_loss is not None and low <= stop_loss
            hit_take = take_profit is not None and high >= take_profit
        else:
            hit_stop = stop_loss is not None and high >= stop_loss
            hit_take = take_profit is not None and low <= take_profit

        if hit_stop and hit_take:
            exit_price = stop_loss
            exit_reason = "STOP_LOSS_AND_TAKE_PROFIT_SAME_CANDLE"
            exit_time = candle_time
            break
        if hit_stop:
            exit_price = stop_loss
            exit_reason = "STOP_LOSS"
            exit_time = candle_time
            break
        if hit_take:
            exit_price = take_profit
            exit_reason = "TAKE_PROFIT"
            exit_time = candle_time
            break

    profit_percent = calculate_profit_percent(signal, entry_price, exit_price)

    return {
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "exit_time": exit_time.isoformat(),
        "profit_percent": profit_percent,
        "max_profit_percent": max_profit,
        "max_drawdown_percent": max_drawdown,
        "is_win": profit_percent > 0,
    }


def backtest_signals(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []

    for signal in signals:
        for hours in CHECK_HOURS:
            target_time = signal["timestamp"] + timedelta(hours=hours)
            if target_time > datetime.now(timezone.utc):
                continue

            candles = get_ohlcv_until(signal["symbol"], signal["timestamp"], target_time)
            evaluation = evaluate_trade(
                signal=signal["signal"],
                entry_price=signal["entry_price"],
                stop_loss=signal["stop_loss"],
                take_profit=signal["take_profit"],
                candles=candles,
            )

            results.append(
                {
                    "timestamp": signal["timestamp"].isoformat(),
                    "symbol": signal["symbol"],
                    "signal": signal["signal"],
                    "raw_confidence": signal["raw_confidence"],
                    "entry_price": signal["entry_price"],
                    "stop_loss": signal["stop_loss"],
                    "take_profit": signal["take_profit"],
                    "rr": signal["rr"],
                    "horizon_hours": hours,
                    "future_time": target_time.isoformat(),
                    "exit_time": evaluation["exit_time"],
                    "exit_price": evaluation["exit_price"],
                    "exit_reason": evaluation["exit_reason"],
                    "profit_percent": evaluation["profit_percent"],
                    "max_profit_percent": evaluation["max_profit_percent"],
                    "max_drawdown_percent": evaluation["max_drawdown_percent"],
                    "is_win": evaluation["is_win"],
                }
            )

    return results


def save_results(results: List[Dict[str, Any]], filename: str = RESULTS_FILE) -> None:
    fieldnames = [
        "timestamp",
        "symbol",
        "signal",
        "raw_confidence",
        "entry_price",
        "stop_loss",
        "take_profit",
        "rr",
        "horizon_hours",
        "future_time",
        "exit_time",
        "exit_price",
        "exit_reason",
        "profit_percent",
        "max_profit_percent",
        "max_drawdown_percent",
        "is_win",
    ]

    with open(filename, mode="w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)


def print_summary(signals: List[Dict[str, Any]], results: List[Dict[str, Any]]) -> None:
    long_count = sum(1 for signal in signals if signal["signal"] == "LONG")
    short_count = sum(1 for signal in signals if signal["signal"] == "SHORT")

    print(f"Всего сигналов: {len(signals)}")
    print(f"LONG сигналов: {long_count}")
    print(f"SHORT сигналов: {short_count}")

    if not results:
        print("Win Rate: 0.00%")
        print("Average Profit: 0.00%")
        print("Best Trade: 0.00%")
        print("Worst Trade: 0.00%")
        return

    profits = [result["profit_percent"] for result in results]
    max_profits = [result["max_profit_percent"] for result in results]
    max_drawdowns = [result["max_drawdown_percent"] for result in results]
    wins = [result for result in results if result["is_win"]]
    take_profit_hits = [result for result in results if result["exit_reason"] == "TAKE_PROFIT"]
    stop_loss_hits = [
        result
        for result in results
        if result["exit_reason"] in {"STOP_LOSS", "STOP_LOSS_AND_TAKE_PROFIT_SAME_CANDLE"}
    ]
    win_rate = len(wins) / len(results) * 100

    print(f"Win Rate: {win_rate:.2f}%")
    print(f"Average Profit: {mean(profits):.2f}%")
    print(f"Best Trade: {max(profits):.2f}%")
    print(f"Worst Trade: {min(profits):.2f}%")
    print(f"Average Max Profit: {mean(max_profits):.2f}%")
    print(f"Average Max Drawdown: {mean(max_drawdowns):.2f}%")
    print(f"TAKE_PROFIT hits: {len(take_profit_hits)}")
    print(f"STOP_LOSS hits: {len(stop_loss_hits)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest saved crypto signals.")
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_MIN_CONFIDENCE,
        help="Minimum raw confidence required to include LONG/SHORT signals.",
    )
    args = parser.parse_args()

    signals = read_signals(min_confidence=args.min_confidence)
    results = backtest_signals(signals)
    save_results(results)
    print(f"MIN_CONFIDENCE: {args.min_confidence:g}%")
    print_summary(signals, results)
    print(f"Результаты сохранены в {RESULTS_FILE}")


if __name__ == "__main__":
    main()
