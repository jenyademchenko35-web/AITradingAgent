"""Simulate blocked trades for Momentum-filtered setups."""

from __future__ import annotations

import csv
import json
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from config import TIMEFRAME

try:
    import ccxt
except ModuleNotFoundError:  # pragma: no cover - runtime environment dependent
    ccxt = None

try:
    import pandas as pd
    from ta.volatility import AverageTrueRange
except ModuleNotFoundError:  # pragma: no cover - runtime environment dependent
    pd = None
    AverageTrueRange = None


BASE_DIR = Path(__file__).resolve().parent
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
OHLCV_CACHE_DIR = BASE_DIR / "ohlcv_cache"

JSON_OUTPUT = BASE_DIR / "blocked_trade_simulation_report.json"
TEXT_OUTPUT = BASE_DIR / "blocked_trade_simulation_summary.txt"
CSV_OUTPUT = BASE_DIR / "blocked_trade_simulation_trades.csv"
CANDIDATES_OUTPUT = BASE_DIR / "blocked_trade_candidates.csv"

SIMULATION_EXPIRY_BARS = 24
ATR_PERIOD = 14
SUPPORTED_BLOCKERS = ("Momentum", "Structure", "Risk", "Trend")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def artifact_path(kind: str, blocker: str) -> Path:
    """Return blocker-scoped artifact path."""
    safe_blocker = blocker.replace("/", "_")
    return BASE_DIR / f"blocked_trade_simulation_{safe_blocker}_{kind}"


def blocker_label(blocker: str) -> str:
    """Return a human-readable blocker label for messages."""
    return blocker if blocker in (*SUPPORTED_BLOCKERS, "ALL") else "Filter"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def parse_time(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    return [
        row for row in rows
        if row and any(row.values()) and row.get("timestamp") != "timestamp"
    ]


def load_cached_history(symbol: str, since_dt: datetime) -> Optional["pd.DataFrame"]:
    """Load OHLCV from a local cache file if present."""
    if pd is None or AverageTrueRange is None:
        return None
    symbol_key = symbol.replace("/", "_").replace(":", "_")
    candidates = [
        OHLCV_CACHE_DIR / f"{symbol_key}_{TIMEFRAME}.csv",
        OHLCV_CACHE_DIR / f"{symbol_key}.csv",
    ]
    cache_path = next((path for path in candidates if path.exists()), None)
    if cache_path is None:
        return None
    df = pd.read_csv(cache_path)
    required = {"timestamp", "open", "high", "low", "close"}
    if not required.issubset(df.columns):
        return None
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df[df["timestamp"] >= pd.Timestamp(since_dt - timedelta(hours=100))]
    if df.empty:
        return None
    if "volume" not in df.columns:
        df["volume"] = 0.0
    atr_indicator = AverageTrueRange(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=ATR_PERIOD,
    )
    df["atr"] = atr_indicator.average_true_range()
    return df.dropna().reset_index(drop=True)


def candidate_direction_from_debug(row: Mapping[str, str]) -> str:
    direction = row.get("direction", "")
    if direction in {"LONG", "SHORT"}:
        return direction
    long_total = safe_float(row.get("long_total"))
    short_total = safe_float(row.get("short_total"))
    if long_total > short_total:
        return "LONG"
    if short_total > long_total:
        return "SHORT"
    return "NEUTRAL"


def nearest_debug_row(
    symbol: str,
    timestamp: datetime,
    debug_by_symbol: Mapping[str, List[Dict[str, str]]],
) -> Optional[Dict[str, str]]:
    best_row: Optional[Dict[str, str]] = None
    best_delta: Optional[float] = None
    for row in debug_by_symbol.get(symbol, []):
        row_time = parse_time(row.get("timestamp", ""))
        if row_time is None:
            continue
        delta = abs((row_time - timestamp).total_seconds())
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_row = row
    return best_row


def build_exchange():
    if ccxt is None:
        raise RuntimeError("ccxt не установлен в текущем окружении.")
    return ccxt.bybit(
        {
            "enableRateLimit": True,
            "timeout": 30000,
            "options": {"defaultType": "spot"},
        }
    )


def fetch_symbol_history(exchange, symbol: str, since_dt: datetime) -> "pd.DataFrame":
    if pd is None or AverageTrueRange is None:
        raise RuntimeError("pandas/ta не установлены в текущем окружении.")

    since_ms = int((since_dt - timedelta(hours=100)).timestamp() * 1000)
    ohlcv = exchange.fetch_ohlcv(
        symbol,
        timeframe=TIMEFRAME,
        since=since_ms,
        limit=1000,
    )
    if not ohlcv:
        raise RuntimeError(f"Не удалось загрузить OHLCV для {symbol}.")

    df = pd.DataFrame(
        ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    atr_indicator = AverageTrueRange(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=ATR_PERIOD,
    )
    df["atr"] = atr_indicator.average_true_range()
    return df.dropna().reset_index(drop=True)


def simulate_trade(
    symbol: str,
    signal_time: Optional[datetime],
    direction: str,
    history: "pd.DataFrame",
) -> Optional[Dict[str, Any]]:
    if signal_time is None:
        return None
    candidates = history[history["timestamp"] <= signal_time]
    if candidates.empty:
        return None
    entry_row = candidates.iloc[-1]
    start_index = int(entry_row.name)
    entry = float(entry_row["close"])
    atr = float(entry_row["atr"])
    if atr <= 0:
        return None

    if direction == "LONG":
        stop_loss = entry - atr
        take_profit = entry + atr * 2
    else:
        stop_loss = entry + atr
        take_profit = entry - atr * 2

    result = "EXPIRED"
    exit_price = float(entry_row["close"])
    exit_time = entry_row["timestamp"]

    future = history.iloc[start_index + 1 : start_index + 1 + SIMULATION_EXPIRY_BARS]
    for _, candle in future.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        exit_time = candle["timestamp"]
        if direction == "LONG":
            if low <= stop_loss:
                result = "LOSS"
                exit_price = stop_loss
                break
            if high >= take_profit:
                result = "WIN"
                exit_price = take_profit
                break
        else:
            if high >= stop_loss:
                result = "LOSS"
                exit_price = stop_loss
                break
            if low <= take_profit:
                result = "WIN"
                exit_price = take_profit
                break
        exit_price = float(candle["close"])

    if result == "WIN":
        r_multiple = 2.0
        pnl_r = 2.0
    elif result == "LOSS":
        r_multiple = -1.0
        pnl_r = -1.0
    else:
        risk = abs(entry - stop_loss)
        reward = exit_price - entry if direction == "LONG" else entry - exit_price
        r_multiple = round((reward / risk) if risk else 0.0, 2)
        pnl_r = r_multiple

    return {
        "symbol": symbol,
        "direction": direction,
        "signal_time": signal_time.isoformat(),
        "entry": round(entry, 6),
        "atr": round(atr, 6),
        "stop_loss": round(stop_loss, 6),
        "take_profit": round(take_profit, 6),
        "exit_time": exit_time.isoformat() if hasattr(exit_time, "isoformat") else str(exit_time),
        "exit_price": round(exit_price, 6),
        "result": result,
        "r_multiple": round(r_multiple, 2),
        "pnl_r": round(pnl_r, 2),
    }


def summarize_trades(trades: List[Mapping[str, Any]]) -> Dict[str, Any]:
    wins = sum(1 for trade in trades if trade.get("result") == "WIN")
    losses = sum(1 for trade in trades if trade.get("result") == "LOSS")
    pnls = [safe_float(trade.get("pnl_r")) for trade in trades]
    gross_profit = sum(max(pnl, 0.0) for pnl in pnls)
    gross_loss = sum(abs(min(pnl, 0.0)) for pnl in pnls)
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "expired": sum(1 for trade in trades if trade.get("result") == "EXPIRED"),
        "winrate": round((wins / len(trades) * 100) if trades else 0.0, 2),
        "average_r": round((sum(pnls) / len(pnls)) if pnls else 0.0, 2),
        "profit_factor": round((gross_profit / gross_loss) if gross_loss else 0.0, 2),
        "max_drawdown": round(max_drawdown, 2),
    }


def save_trades(trades: List[Mapping[str, Any]], blocker: str) -> None:
    path = artifact_path("trades.csv", blocker)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "symbol", "direction", "signal_time", "entry", "atr",
                "stop_loss", "take_profit", "exit_time", "exit_price",
                "result", "r_multiple", "pnl_r",
            ]
        )
        for trade in trades:
            writer.writerow([trade.get(key, "") for key in [
                "symbol", "direction", "signal_time", "entry", "atr",
                "stop_loss", "take_profit", "exit_time", "exit_price",
                "result", "r_multiple", "pnl_r",
            ]])


def save_candidates(candidates: List[Mapping[str, Any]], blocker: str) -> None:
    """Save blocked trade candidates for later simulation."""
    path = (
        CANDIDATES_OUTPUT if blocker == "Momentum"
        else BASE_DIR / f"blocked_trade_candidates_{blocker}.csv"
    )
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "timestamp",
                "symbol",
                "candidate_direction",
                "primary_blocker",
                "potential_score",
                "actual_score",
                "lost_score",
                "entry",
                "atr",
                "reason",
            ]
        )
        for item in candidates:
            writer.writerow(
                [
                    item.get("timestamp", ""),
                    item.get("symbol", ""),
                    item.get("direction", ""),
                    item.get("primary_blocker", ""),
                    item.get("potential_score", ""),
                    item.get("actual_score", ""),
                    item.get("lost_score", ""),
                    item.get("entry", ""),
                    item.get("atr", ""),
                    item.get("reason", ""),
                ]
            )


def simulate_for_blocker(blocker: str) -> Dict[str, Any]:
    diagnostics_rows = read_csv_rows(DIAGNOSTICS_FILE)
    debug_rows = read_csv_rows(DEBUG_FILE)
    _signal_rows = read_csv_rows(SIGNALS_FILE)

    candidates = []
    debug_by_symbol: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in debug_rows:
        debug_by_symbol[row.get("symbol", "")].append(row)
    for rows in debug_by_symbol.values():
        rows.sort(key=lambda item: item.get("timestamp", ""))

    for row in diagnostics_rows:
        if row.get("decision") not in {"NO TRADE", "WAIT"}:
            continue
        if row.get("primary_blocker") != blocker:
            continue
        if safe_float(row.get("potential_score")) < 23:
            continue
        ts = parse_time(row.get("timestamp", ""))
        symbol = row.get("symbol", "")
        if ts is None or not symbol:
            continue
        debug_row = nearest_debug_row(symbol, ts, debug_by_symbol)
        if debug_row is None:
            continue
        direction = candidate_direction_from_debug(debug_row)
        if direction not in {"LONG", "SHORT"}:
            continue
        candidates.append(
            {
                "timestamp": ts.isoformat(),
                "symbol": symbol,
                "direction": direction,
                "primary_blocker": row.get("primary_blocker", ""),
                "potential_score": safe_float(row.get("potential_score")),
                "actual_score": safe_float(debug_row.get("score")),
                "lost_score": safe_float(row.get("lost_score")),
                "entry": "",
                "atr": "",
                "reason": "Momentum blocked near-actionable setup.",
            }
        )

    if ccxt is None or pd is None or AverageTrueRange is None:
        save_candidates(candidates, blocker)
        return {
            "generated_at": utc_now(),
            "blocker": blocker,
            "status": "WAITING_FOR_DATA",
            "error": "ccxt/pandas/ta недоступны в текущем окружении.",
            "blocked_momentum_candidates": len(candidates),
            "candidates_file": str(
                CANDIDATES_OUTPUT if blocker == "Momentum"
                else BASE_DIR / f"blocked_trade_candidates_{blocker}.csv"
            ),
            "recommendation": (
                f"Нужно запустить симулятор для {blocker_label(blocker)} "
                "в среде с OHLCV-данными или добавить локальный cache."
            ),
        }

    if not candidates:
        return {
            "generated_at": utc_now(),
            "blocker": blocker,
            "status": "OK",
            "blocked_momentum_candidates": 0,
            "summary": summarize_trades([]),
            "by_symbol": {},
            "by_direction": {},
            "recommendation": (
                f"Подходящих blocked {blocker_label(blocker)}-сделок не найдено."
            ),
        }

    try:
        earliest_by_symbol: Dict[str, datetime] = {}
        for item in candidates:
            symbol = item["symbol"]
            ts = parse_time(item["timestamp"])
            if ts is None:
                continue
            if symbol not in earliest_by_symbol or ts < earliest_by_symbol[symbol]:
                earliest_by_symbol[symbol] = ts

        history_by_symbol: Dict[str, "pd.DataFrame"] = {}
        fetch_errors: Dict[str, str] = {}
        exchange = None
        if ccxt is not None and pd is not None and AverageTrueRange is not None:
            try:
                exchange = build_exchange()
            except Exception as exc:
                fetch_errors["exchange"] = str(exc)

        for symbol, ts in earliest_by_symbol.items():
            history = None
            live_error = ""
            if exchange is not None:
                try:
                    history = fetch_symbol_history(exchange, symbol, ts)
                except Exception as exc:
                    live_error = str(exc)
            if history is None:
                history = load_cached_history(symbol, ts)
            if history is None:
                fetch_errors[symbol] = live_error or "Локальный OHLCV-cache не найден."
                continue
            history_by_symbol[symbol] = history
    except Exception as exc:
        return {
            "generated_at": utc_now(),
            "status": "ERROR",
            "error": str(exc),
            "blocked_momentum_candidates": len(candidates),
            "recommendation": (
                f"Не удалось загрузить OHLCV для blocked "
                f"{blocker_label(blocker)} trade simulation."
            ),
        }

    if not history_by_symbol:
        save_candidates(candidates, blocker)
        return {
            "generated_at": utc_now(),
            "blocker": blocker,
            "status": "WAITING_FOR_DATA",
            "error": "OHLCV недоступен ни через ccxt, ни через локальный cache.",
            "blocked_momentum_candidates": len(candidates),
            "simulated_trades": 0,
            "candidates_file": str(
                CANDIDATES_OUTPUT if blocker == "Momentum"
                else BASE_DIR / f"blocked_trade_candidates_{blocker}.csv"
            ),
            "fetch_errors": fetch_errors,
            "recommendation": (
                f"Нужно запустить симулятор для {blocker_label(blocker)} "
                "в среде с доступом к Bybit или добавить локальный OHLCV-cache."
            ),
        }

    simulated_trades: List[Dict[str, Any]] = []
    for item in candidates:
        if item["symbol"] not in history_by_symbol:
            continue
        trade = simulate_trade(
            item["symbol"],
            parse_time(item["timestamp"]),
            item["direction"],
            history_by_symbol[item["symbol"]],
        )
        if trade is not None:
            simulated_trades.append(trade)
            item["entry"] = trade["entry"]
            item["atr"] = trade["atr"]

    save_candidates(candidates, blocker)

    overall = summarize_trades(simulated_trades)
    by_symbol = {
        symbol: summarize_trades([trade for trade in simulated_trades if trade["symbol"] == symbol])
        for symbol in sorted({trade["symbol"] for trade in simulated_trades})
    }
    by_direction = {
        direction: summarize_trades([trade for trade in simulated_trades if trade["direction"] == direction])
        for direction in sorted({trade["direction"] for trade in simulated_trades})
    }

    best_symbol = "N/A"
    worst_symbol = "N/A"
    if by_symbol:
        best_symbol = max(by_symbol, key=lambda key: (by_symbol[key]["profit_factor"], by_symbol[key]["average_r"]))
        worst_symbol = min(by_symbol, key=lambda key: (by_symbol[key]["profit_factor"], by_symbol[key]["average_r"]))

    blocker_name = blocker_label(blocker)
    if overall["profit_factor"] > 1.0 and overall["winrate"] >= 50:
        recommendation = (
            f"{blocker_name} можно осторожно ослаблять в backtest: "
            "blocked trades выглядят жизнеспособно."
        )
    else:
        recommendation = (
            f"{blocker_name} пока ослаблять рано: "
            "blocked trades не показали уверенного преимущества."
        )

    return {
        "generated_at": utc_now(),
        "blocker": blocker,
        "status": "OK",
        "notes": [
            "Simulation uses 1h OHLCV, ATR(14), SL=1 ATR, TP=2 ATR, expiry=24 bars.",
            "This is an analytical scenario and does not change live trading logic.",
        ],
        "blocked_momentum_candidates": len(candidates),
        "simulated_trades": len(simulated_trades),
        "summary": overall,
        "by_symbol": by_symbol,
        "by_direction": by_direction,
        "best_symbol": best_symbol,
        "worst_symbol": worst_symbol,
        "recommendation": recommendation,
        "trades": simulated_trades,
        "candidates_file": str(
            CANDIDATES_OUTPUT if blocker == "Momentum"
            else BASE_DIR / f"blocked_trade_candidates_{blocker}.csv"
        ),
    }


def build_report(blocker: str) -> Dict[str, Any]:
    """Build report for a blocker or all blockers."""
    if blocker != "ALL":
        return simulate_for_blocker(blocker)

    per_blocker = {
        name: simulate_for_blocker(name)
        for name in SUPPORTED_BLOCKERS
    }
    ok_reports = [item for item in per_blocker.values() if item.get("status") == "OK"]
    waiting_reports = [item for item in per_blocker.values() if item.get("status") == "WAITING_FOR_DATA"]
    total_candidates = sum(item.get("blocked_momentum_candidates", 0) for item in per_blocker.values())
    total_simulated = sum(item.get("simulated_trades", 0) for item in per_blocker.values())
    all_trades: List[Mapping[str, Any]] = []
    for item in ok_reports:
        all_trades.extend(item.get("trades", []))

    status = "OK" if ok_reports else ("WAITING_FOR_DATA" if waiting_reports else "ERROR")
    summary = summarize_trades(all_trades)
    by_blocker = {
        name: {
            "status": item.get("status", "N/A"),
            "blocked_candidates": item.get("blocked_momentum_candidates", 0),
            "simulated_trades": item.get("simulated_trades", 0),
            "summary": item.get("summary", {}),
            "best_symbol": item.get("best_symbol", "N/A"),
            "worst_symbol": item.get("worst_symbol", "N/A"),
        }
        for name, item in per_blocker.items()
    }
    recommendation = (
        "Не ослаблять фильтры автоматически: сначала сравнить blocker-specific результаты."
        if all_trades else
        "Недостаточно OHLCV-данных для общего blocked-trade simulation."
    )
    return {
        "generated_at": utc_now(),
        "blocker": "ALL",
        "status": status,
        "blocked_momentum_candidates": total_candidates,
        "simulated_trades": total_simulated,
        "summary": summary,
        "by_blocker": by_blocker,
        "recommendation": recommendation,
        "trades": all_trades,
    }


def save_report(report: Dict[str, Any], blocker: str) -> None:
    path = artifact_path("report.json", blocker)
    with path.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)


def save_summary(report: Dict[str, Any], blocker: str) -> None:
    path = artifact_path("summary.txt", blocker)
    lines = [
        "AITradingAgent Blocked Trade Simulation",
        f"Generated at: {report.get('generated_at', '')}",
        f"Blocker: {report.get('blocker', blocker)}",
        f"Status: {report.get('status', 'N/A')}",
        f"Blocked Momentum candidates: {report.get('blocked_momentum_candidates', 0)}",
        f"Simulated trades: {report.get('simulated_trades', 0)}",
    ]
    if report.get("status") == "OK":
        summary = report.get("summary", {})
        lines.extend(
            [
                f"Winrate: {summary.get('winrate', 0)}%",
                f"Average R: {summary.get('average_r', 0)}",
                f"Profit Factor: {summary.get('profit_factor', 0)}",
                f"Max Drawdown: {summary.get('max_drawdown', 0)}",
                f"Best symbol: {report.get('best_symbol', 'N/A')}",
                f"Worst symbol: {report.get('worst_symbol', 'N/A')}",
                f"Recommendation: {report.get('recommendation', '')}",
            ]
        )
    else:
        lines.append(f"Error: {report.get('error', 'N/A')}")
        if report.get("candidates_file"):
            lines.append(f"Candidates file: {report.get('candidates_file')}")
    if blocker == "ALL" and report.get("by_blocker"):
        lines.append("")
        lines.append("By blocker:")
        for name, item in report["by_blocker"].items():
            lines.append(
                f"- {name}: status={item.get('status')} | "
                f"candidates={item.get('blocked_candidates', 0)} | "
                f"simulated={item.get('simulated_trades', 0)}"
            )
    path.write_text("\n".join(lines), encoding="utf-8")


def print_summary(report: Dict[str, Any], blocker: str) -> None:
    print("Blocked Trade Simulator")
    print(f"Blocker         : {report.get('blocker', blocker)}")
    print(f"Status          : {report.get('status', 'N/A')}")
    print(f"Candidates      : {report.get('blocked_momentum_candidates', 0)}")
    print(f"Simulated       : {report.get('simulated_trades', 0)}")
    print(f"JSON report     : {artifact_path('report.json', blocker)}")
    print(f"TXT summary     : {artifact_path('summary.txt', blocker)}")
    print(f"CSV trades      : {artifact_path('trades.csv', blocker)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--blocker",
        default="Momentum",
        choices=[*SUPPORTED_BLOCKERS, "ALL"],
        help="Which blocker to simulate.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    blocker = args.blocker
    report = build_report(blocker)
    save_report(report, blocker)
    save_summary(report, blocker)
    save_trades(report.get("trades", []), blocker)
    print_summary(report, blocker)


if __name__ == "__main__":
    main()
