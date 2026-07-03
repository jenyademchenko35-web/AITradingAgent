import argparse
import csv
import json
import os
import time
from datetime import datetime, timezone
from typing import Dict, Any

import ccxt
import pandas as pd
import requests
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange


SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
TIMEFRAMES = ["1h", "4h", "1d"]
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen3:14b"
OHLCV_LIMIT = 200
SIGNALS_FILE = "signals.csv"
STATS_FILE = "agent_stats.json"
SETUP_HISTORY_FILE = "setup_history.csv"
ACTIVE_SETUPS_FILE = "active_setups.json"
RUN_INTERVAL_SECONDS = 3600
SETUP_COOLDOWN_HOURS = 6
SCORE_THRESHOLDS = {
    "HIGH PRIORITY": 90,
    "SETUP": 80,
    "WATCH": 60,
    "WAIT": 40,
}
STATS_KEYS = ["runs", "high_priority", "setup", "watch", "wait", "no_trade"]
SIGNAL_FIELDNAMES = [
    "timestamp",
    "setup_id",
    "setup_active",
    "symbol",
    "price",
    "direction",
    "score",
    "trend_score",
    "structure_score",
    "entry_score",
    "rr_score",
    "distance_to_entry_zone",
    "distance_penalty",
    "raw_signal",
    "raw_confidence",
    "final_signal",
    "final_confidence",
    "signal",
    "confidence",
    "entry",
    "stop_loss",
    "take_profit",
    "rr",
    "reason",
    "comment",
    "notify",
    "timeframe",
    "atr_1h",
    "atr_4h",
    "atr_1d",
    "price_position_1h",
    "price_position_4h",
    "price_position_1d",
    "trend_ema_1h",
    "trend_ema_4h",
    "trend_ema_1d",
    "trend_macd_1h",
    "trend_macd_4h",
    "trend_macd_1d",
]

EXCHANGE = ccxt.bybit(
    {
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    }
)


def get_market_data(symbol: str, timeframe: str, limit: int = OHLCV_LIMIT) -> Dict[str, Any]:
    """Fetch Bybit OHLCV data and calculate indicators plus 20-candle market structure."""
    ohlcv = EXCHANGE.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    if not ohlcv:
        raise RuntimeError(f"No OHLCV data returned for {symbol} {timeframe}")

    df = pd.DataFrame(
        ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")

    df["rsi14"] = RSIIndicator(close=df["close"], window=14).rsi()
    df["ema20"] = EMAIndicator(close=df["close"], window=20).ema_indicator()
    df["ema50"] = EMAIndicator(close=df["close"], window=50).ema_indicator()
    df["atr14"] = AverageTrueRange(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=14,
    ).average_true_range()

    macd_indicator = MACD(close=df["close"])
    df["macd"] = macd_indicator.macd()
    df["macd_signal"] = macd_indicator.macd_signal()
    df["macd_diff"] = macd_indicator.macd_diff()
    df["highest_20"] = df["high"].rolling(window=20).max()
    df["lowest_20"] = df["low"].rolling(window=20).min()
    df["price_position"] = (
        (df["close"] - df["lowest_20"]) / (df["highest_20"] - df["lowest_20"]) * 100
    )

    latest = df.iloc[-1]
    price_position = latest["price_position"]
    if pd.isna(price_position):
        price_position = 50.0

    trend_ema = "BULLISH" if latest["ema20"] > latest["ema50"] else "BEARISH"
    trend_macd = "BULLISH" if latest["macd"] > latest["macd_signal"] else "BEARISH"

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": latest["timestamp"].isoformat(),
        "open": float(latest["open"]),
        "high": float(latest["high"]),
        "low": float(latest["low"]),
        "close": float(latest["close"]),
        "volume": float(latest["volume"]),
        "rsi14": float(latest["rsi14"]),
        "ema20": float(latest["ema20"]),
        "ema50": float(latest["ema50"]),
        "atr14": float(latest["atr14"]),
        "macd": float(latest["macd"]),
        "macd_signal": float(latest["macd_signal"]),
        "macd_diff": float(latest["macd_diff"]),
        "highest_20": float(latest["highest_20"]),
        "lowest_20": float(latest["lowest_20"]),
        "price_position": float(max(0.0, min(100.0, price_position))),
        "trend_ema": trend_ema,
        "trend_macd": trend_macd,
    }


def score_market_setup(symbol: str, market_data: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Make the trading decision in Python using the deterministic score model."""
    price = market_data["1h"]["close"]
    atr = market_data["1h"]["atr14"]
    entry = price

    ema_trends = [market_data[timeframe]["trend_ema"] for timeframe in TIMEFRAMES]
    direction = _get_aligned_direction(ema_trends)
    trend_score = 40 if direction else 0

    structure_score = 0
    entry_score = 0
    stop_loss = None
    take_profit = None
    rr = None

    if direction:
        structure_score = 25 if _structure_matches_direction(direction, market_data) else 0
        entry_score = _calculate_entry_score(direction, market_data)
        risk_plan = _build_risk_plan(direction, entry, atr, None, None, None)
        stop_loss = risk_plan["stop_loss"]
        take_profit = risk_plan["take_profit"]
        rr = risk_plan["rr"]

    rr_score = 15 if rr is not None and rr >= 2 else 0
    distance_to_entry_zone = _distance_to_entry_zone(direction, market_data)

    distance_penalty = 0

    if distance_to_entry_zone is not None:
        if distance_to_entry_zone > 20:
            distance_penalty = 20
        elif distance_to_entry_zone > 10:
            distance_penalty = 10
        elif distance_to_entry_zone > 5:
            distance_penalty = 5

    daily_macd_penalty = _daily_macd_penalty(direction, market_data)
    score = max(
        0,
        trend_score
        + structure_score
        + entry_score
        + rr_score
        - distance_penalty
        - daily_macd_penalty,
    )
    final_signal = _signal_from_score(
        score=score,
        trend_score=trend_score,
        structure_score=structure_score,
        entry_score=entry_score,
        rr_score=rr_score,
    )
    reason = _build_reason(
        direction=direction,
        trend_score=trend_score,
        structure_score=structure_score,
        entry_score=entry_score,
        rr_score=rr_score,
    )
    if daily_macd_penalty:
        reason += " Конфликт с дневным MACD."

    if final_signal == "NO TRADE":
        stop_loss = None
        take_profit = None
        rr = None

    comment = _build_indicator_comment(market_data)

    return {
        "symbol": symbol,
        "setup_id": build_setup_id(symbol, direction),
        "setup_active": False,
        "price": price,
        "direction": direction or "NO TRADE",
        "score": score,
        "trend_score": trend_score,
        "structure_score": structure_score,
        "entry_score": entry_score,
        "rr_score": rr_score,
        "distance_to_entry_zone": distance_to_entry_zone,
        "distance_penalty": distance_penalty,
        "daily_macd_penalty": daily_macd_penalty,
        "raw_signal": direction or "NO TRADE",
        "raw_confidence": f"{score:g}%",
        "final_signal": final_signal,
        "final_confidence": f"{score:g}%",
        "signal": final_signal,
        "confidence": f"{score:g}%",
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "rr": rr,
        "reason": reason,
        "comment": comment,
        "notify": should_send_notification(final_signal, score, entry_score),
    }


def build_setup_id(symbol: str, direction: str | None) -> str:
    if not direction:
        return ""
    base_symbol = symbol.split("/")[0].replace(":", "_")
    return f"{base_symbol}_{direction}"


def _daily_macd_penalty(
    direction: str | None,
    market_data: Dict[str, Dict[str, Any]],
) -> int:
    daily_macd = market_data["1d"]["trend_macd"]
    if direction == "SHORT" and daily_macd == "BULLISH":
        return 15
    if direction == "LONG" and daily_macd == "BEARISH":
        return 15
    return 0


def _get_aligned_direction(ema_trends: list[str]) -> str | None:
    if all(trend == "BULLISH" for trend in ema_trends):
        return "LONG"
    if all(trend == "BEARISH" for trend in ema_trends):
        return "SHORT"
    return None


def _structure_matches_direction(
    direction: str,
    market_data: Dict[str, Dict[str, Any]],
) -> bool:
    positions = [market_data[timeframe]["price_position"] for timeframe in TIMEFRAMES]
    if direction == "LONG":
        return all(position >= 50 for position in positions)
    return all(position <= 50 for position in positions)


def _calculate_entry_score(direction: str, market_data: Dict[str, Dict[str, Any]]) -> int:
    return 20 if _distance_to_entry_zone(direction, market_data) == 0 else 0


def _distance_to_entry_zone(
    direction: str | None,
    market_data: Dict[str, Dict[str, Any]],
) -> float | None:
    if not direction:
        return None

    position_1h = market_data["1h"]["price_position"]
    if direction == "LONG":
        zone_low = 20
        zone_high = 60
    else:
        zone_low = 40
        zone_high = 80

    if position_1h < zone_low:
        return zone_low - position_1h
    if position_1h > zone_high:
        return position_1h - zone_high
    return 0.0


def _signal_from_score(
    score: float,
    trend_score: int,
    structure_score: int,
    entry_score: int,
    rr_score: int,
) -> str:
    if entry_score < 20:
        return "WATCH" if score >= SCORE_THRESHOLDS["WATCH"] else _signal_below_watch(score)

    if (
        score == 100
        and trend_score == 40
        and structure_score == 25
        and entry_score == 20
        and rr_score == 15
    ):
        return "HIGH PRIORITY"

    if score >= SCORE_THRESHOLDS["SETUP"]:
        return "SETUP"
    if score >= SCORE_THRESHOLDS["WATCH"]:
        return "WATCH"
    return _signal_below_watch(score)


def _signal_below_watch(score: float) -> str:
    if score >= SCORE_THRESHOLDS["WAIT"]:
        return "WAIT"
    return "NO TRADE"


def _build_reason(
    direction: str | None,
    trend_score: int,
    structure_score: int,
    entry_score: int,
    rr_score: int,
) -> str:
    missing = []
    if trend_score == 0:
        missing.append("EMA тренды 1d, 4h и 1h не совпадают")
    if direction and structure_score == 0:
        missing.append("структура рынка не совпадает с направлением тренда")
    if direction and entry_score == 0:
        missing.append("не хватает отката для входа")
    if direction and rr_score == 0:
        missing.append("RR ниже 2")

    if not direction:
        return "EMA тренды 1d, 4h и 1h не совпадают."

    if not missing:
        return "сетап полностью сформирован."
    if entry_score < 20:
        confirmed = []
        if trend_score == 40:
            confirmed.append("тренд подтверждён")
        if structure_score == 25:
            confirmed.append("структура подтверждена")
        if rr_score == 15:
            confirmed.append("RR подходит")
        confirmed.append("ожидаем откат к зоне входа")
        return ", ".join(confirmed) + "."
    return "; ".join(missing) + "."


def _build_indicator_comment(market_data: Dict[str, Dict[str, Any]]) -> str:
    parts = []

    for timeframe in TIMEFRAMES:
        data = market_data[timeframe]
        parts.append(
            f"{timeframe}: RSI={data['rsi14']:.2f}, MACD={data['trend_macd'].lower()}"
        )

    warnings = []

    rsi_1h = market_data["1h"]["rsi14"]

    if rsi_1h < 25:
        warnings.append("⚠️ рынок перепродан, возможен отскок")

    if rsi_1h > 75:
        warnings.append("⚠️ рынок перекуплен, возможна коррекция")

    comment = "Комментарий: " + "; ".join(parts) + "."

    if warnings:
        comment += " " + " ".join(warnings)

    return comment


def should_send_notification(signal: str, score: float, entry_score: int) -> bool:
    if entry_score < 20:
        return False
    return score >= 80 or signal in {"SETUP", "HIGH PRIORITY"}


def apply_setup_cooldown(
    signal_data: Dict[str, Any],
    filename: str = ACTIVE_SETUPS_FILE,
) -> None:
    setup_id = signal_data.get("setup_id", "")
    if not setup_id:
        signal_data["setup_active"] = False
        return

    now = datetime.now(timezone.utc)
    active_setups = load_active_setups(filename)
    previous_timestamp = _parse_setup_timestamp(active_setups.get(setup_id))
    is_active = False

    if previous_timestamp:
        elapsed = now - previous_timestamp
        is_active = elapsed.total_seconds() < SETUP_COOLDOWN_HOURS * 3600

    if signal_data["notify"]:
        if is_active:
            signal_data["notify"] = False
            signal_data["reason"] += " Сетап уже активен."
        else:
            active_setups[setup_id] = _format_setup_timestamp(now)
            save_active_setups(active_setups, filename)
            is_active = True

    signal_data["setup_active"] = is_active


def load_active_setups(filename: str = ACTIVE_SETUPS_FILE) -> Dict[str, str]:
    if not os.path.exists(filename):
        return {}

    with open(filename, mode="r", encoding="utf-8") as active_setups_file:
        try:
            active_setups = json.load(active_setups_file)
        except json.JSONDecodeError:
            return {}

    if not isinstance(active_setups, dict):
        return {}

    return {
        str(setup_id): str(timestamp)
        for setup_id, timestamp in active_setups.items()
        if isinstance(setup_id, str) and isinstance(timestamp, str)
    }


def save_active_setups(
    active_setups: Dict[str, str],
    filename: str = ACTIVE_SETUPS_FILE,
) -> None:
    with open(filename, mode="w", encoding="utf-8") as active_setups_file:
        json.dump(active_setups, active_setups_file, ensure_ascii=False, indent=2)
        active_setups_file.write("\n")


def _parse_setup_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_setup_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def ask_qwen_for_comment(signal_data: Dict[str, Any]) -> str:
    """Optionally ask Qwen for commentary only; it never changes the Python decision."""
    prompt = f"""
Сделай короткий комментарий на русском к торговому сетапу.
Не меняй SIGNAL, SCORE, DIRECTION и уровни.

SIGNAL: {signal_data["final_signal"]}
DIRECTION: {signal_data["direction"]}
SCORE: {signal_data["score"]}
REASON: {signal_data["reason"]}
{signal_data["comment"]}
""".strip()

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1},
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json().get("response", "").strip()


def analyze_symbol(symbol: str) -> Dict[str, Any]:
    """Analyze one symbol across all configured timeframes."""
    market_data = {}
    for timeframe in TIMEFRAMES:
        market_data[timeframe] = get_market_data(symbol, timeframe)

    signal_data = score_market_setup(symbol, market_data)
    apply_setup_cooldown(signal_data)
    if os.getenv("AI_AGENT_USE_QWEN_COMMENT") == "1":
        try:
            qwen_comment = ask_qwen_for_comment(signal_data)
            if qwen_comment:
                signal_data["comment"] = qwen_comment
        except Exception as exc:
            signal_data["comment"] = (
                f"{signal_data['comment']} Qwen comment unavailable: {exc}"
            )

    save_signal(signal_data, market_data)
    save_setup_history(signal_data)
    update_agent_stats(signal_data["final_signal"])
    return signal_data


def save_signal(
    signal_data: Dict[str, Any],
    market_data: Dict[str, Dict[str, Any]] | None = None,
    filename: str = SIGNALS_FILE,
) -> None:
    _ensure_signal_file_schema(filename)

    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "setup_id": signal_data.get("setup_id", ""),
        "setup_active": "YES" if signal_data.get("setup_active") else "NO",
        "symbol": signal_data["symbol"],
        "price": signal_data["price"],
        "direction": signal_data["direction"],
        "score": signal_data["score"],
        "trend_score": signal_data["trend_score"],
        "structure_score": signal_data["structure_score"],
        "entry_score": signal_data["entry_score"],
        "rr_score": signal_data["rr_score"],
        "distance_to_entry_zone": signal_data["distance_to_entry_zone"],
        "distance_penalty": signal_data.get("distance_penalty", 0),
        "raw_signal": signal_data["raw_signal"],
        "raw_confidence": signal_data["raw_confidence"],
        "final_signal": signal_data["final_signal"],
        "final_confidence": signal_data["final_confidence"],
        "signal": signal_data["final_signal"],
        "confidence": signal_data["final_confidence"],
        "entry": signal_data["entry"],
        "stop_loss": signal_data["stop_loss"],
        "take_profit": signal_data["take_profit"],
        "rr": signal_data["rr"],
        "reason": signal_data["reason"],
        "comment": signal_data["comment"],
        "notify": "YES" if signal_data["notify"] else "NO",
        "timeframe": "multi",
    }

    for timeframe in TIMEFRAMES:
        suffix = timeframe.replace("h", "h").replace("d", "d")
        data = market_data.get(timeframe, {}) if market_data else {}

        row[f"atr_{suffix}"] = data.get("atr14", "")
        row[f"price_position_{suffix}"] = data.get("price_position", "")
        row[f"trend_ema_{suffix}"] = data.get("trend_ema", "")
        row[f"trend_macd_{suffix}"] = data.get("trend_macd", "")

    with open(filename, mode="a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=SIGNAL_FIELDNAMES)
        writer.writerow(row)


def save_setup_history(signal_data: Dict[str, Any]) -> None:
    if not signal_data.get("notify"):
        return

    file_exists = os.path.exists(SETUP_HISTORY_FILE)

    fieldnames = [
        "timestamp",
        "setup_id",
        "symbol",
        "direction",
        "signal",
        "score",
        "distance_to_entry_zone",
    ]

    with open(
        SETUP_HISTORY_FILE,
        mode="a",
        newline="",
        encoding="utf-8",
    ) as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        writer.writerow(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "setup_id": signal_data.get("setup_id", ""),
                "symbol": signal_data.get("symbol", ""),
                "direction": signal_data.get("direction", ""),
                "signal": signal_data.get("final_signal", ""),
                "score": signal_data.get("score", ""),
                "distance_to_entry_zone": signal_data.get(
                    "distance_to_entry_zone",
                    "",
                ),
            }
        )


def _ensure_signal_file_schema(filename: str) -> None:
    if not os.path.exists(filename):
        with open(filename, mode="w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=SIGNAL_FIELDNAMES)
            writer.writeheader()
        return

    with open(filename, mode="r", newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames == SIGNAL_FIELDNAMES:
            return
        rows = list(reader)

    migrated_rows = []
    for row in rows:
        migrated_row = {field: row.get(field, "") for field in SIGNAL_FIELDNAMES}
        previous_signal = row.get("signal", "")
        previous_confidence = row.get("confidence", "")
        migrated_row["raw_signal"] = row.get("raw_signal", previous_signal)
        migrated_row["raw_confidence"] = row.get("raw_confidence", previous_confidence)
        migrated_row["final_signal"] = row.get("final_signal", previous_signal)
        migrated_row["final_confidence"] = row.get("final_confidence", previous_confidence)
        migrated_row["signal"] = row.get("signal", previous_signal)
        migrated_row["confidence"] = row.get("confidence", previous_confidence)
        migrated_row["direction"] = row.get("direction", previous_signal)
        migrated_row["score"] = row.get("score", previous_confidence)
        migrated_rows.append(migrated_row)

    with open(filename, mode="w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=SIGNAL_FIELDNAMES)
        writer.writeheader()
        writer.writerows(migrated_rows)


def load_agent_stats(filename: str = STATS_FILE) -> Dict[str, int]:
    if not os.path.exists(filename):
        return {key: 0 for key in STATS_KEYS}

    with open(filename, mode="r", encoding="utf-8") as stats_file:
        try:
            loaded_stats = json.load(stats_file)
        except json.JSONDecodeError:
            loaded_stats = {}

    stats = {key: 0 for key in STATS_KEYS}
    for key in STATS_KEYS:
        value = loaded_stats.get(key, 0)
        stats[key] = int(value) if isinstance(value, (int, float)) else 0
    return stats


def update_agent_stats(signal: str, filename: str = STATS_FILE) -> None:
    stats = load_agent_stats(filename)
    stats["runs"] += 1
    stats[_stats_key_for_signal(signal)] += 1

    with open(filename, mode="w", encoding="utf-8") as stats_file:
        json.dump(stats, stats_file, ensure_ascii=False, indent=2)
        stats_file.write("\n")


def _stats_key_for_signal(signal: str) -> str:
    return signal.lower().replace(" ", "_")


def _build_risk_plan(
    signal: str,
    entry: float,
    atr: float,
    stop_loss: float | None,
    take_profit: float | None,
    rr: float | None,
) -> Dict[str, float]:
    fallback_risk = max(atr * 1.5, entry * 0.002)

    if signal == "LONG":
        if stop_loss is None or stop_loss >= entry:
            stop_loss = entry - fallback_risk
        risk = entry - stop_loss
        if take_profit is None or take_profit <= entry:
            take_profit = entry + risk * 2
        reward = take_profit - entry
    else:
        if stop_loss is None or stop_loss <= entry:
            stop_loss = entry + fallback_risk
        risk = stop_loss - entry
        if take_profit is None or take_profit >= entry:
            take_profit = entry - risk * 2
        reward = entry - take_profit

    calculated_rr = reward / risk if risk > 0 else 0.0
    if rr is None or rr <= 0:
        rr = calculated_rr

    return {
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "rr": rr,
    }


def _format_signal(signal_data: Dict[str, Any]) -> str:
    stop_loss = _format_optional_number(signal_data["stop_loss"])
    take_profit = _format_optional_number(signal_data["take_profit"])
    rr = _format_optional_number(signal_data["rr"], decimals=2)
    distance_to_entry_zone = _format_optional_number(
        signal_data["distance_to_entry_zone"],
        decimals=2,
    )
    distance_text = (
        "N/A" if distance_to_entry_zone == "N/A" else f"{distance_to_entry_zone}%"
    )

    return (
        f"PRICE: {signal_data['price']:.4f}\n"
        f"SETUP ID: {signal_data.get('setup_id', '') or 'N/A'}\n"
        f"SETUP ACTIVE: {'YES' if signal_data.get('setup_active') else 'NO'}\n"
        f"SIGNAL: {signal_data['final_signal']}\n"
        f"DIRECTION: {signal_data['direction']}\n"
        f"SCORE: {signal_data['score']:.0f}\n"
        f"TREND SCORE: {signal_data['trend_score']}/40\n"
        f"STRUCTURE SCORE: {signal_data['structure_score']}/25\n"
        f"ENTRY SCORE: {signal_data['entry_score']}/20\n"
        f"RR SCORE: {signal_data['rr_score']}/15\n"
        f"DISTANCE TO ENTRY ZONE: {distance_text}\n"
        f"DISTANCE PENALTY: {signal_data.get('distance_penalty', 0)}\n"
        f"ENTRY: {signal_data['entry']:.4f}\n"
        f"STOP_LOSS: {stop_loss}\n"
        f"TAKE_PROFIT: {take_profit}\n"
        f"RR: {rr}\n"
        f"NOTIFY: {'YES' if signal_data['notify'] else 'NO'}\n"
        f"REASON: {signal_data['reason']}\n"
        f"{signal_data['comment']}"
    )


def _format_optional_number(value: float | None, decimals: int = 4) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f}"


def analyze_all_symbols() -> None:
    for symbol in SYMBOLS:
        print(f"\n===== {symbol} =====")
        try:
            print(_format_signal(analyze_symbol(symbol)))
        except Exception as exc:
            price = 0.0
            signal_data = {
                "symbol": symbol,
                "setup_id": "",
                "setup_active": False,
                "price": price,
                "direction": "NO TRADE",
                "score": 0,
                "trend_score": 0,
                "structure_score": 0,
                "entry_score": 0,
                "rr_score": 0,
                "distance_to_entry_zone": None,
                "distance_penalty": 0,
                "raw_signal": "NO TRADE",
                "raw_confidence": "0%",
                "final_signal": "NO TRADE",
                "final_confidence": "0%",
                "signal": "NO TRADE",
                "confidence": "0%",
                "entry": price,
                "stop_loss": None,
                "take_profit": None,
                "rr": None,
                "reason": f"Ошибка анализа {symbol}: {exc}",
                "comment": "Комментарий: RSI/MACD недоступны из-за ошибки анализа.",
                "notify": False,
            }
            save_signal(signal_data)
            update_agent_stats(signal_data["final_signal"])
            print(_format_signal(signal_data))


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-timeframe crypto signal agent.")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run analysis every hour and append results to signals.csv.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=RUN_INTERVAL_SECONDS,
        help="Loop interval in seconds. Default: 3600.",
    )
    args = parser.parse_args()

    if not args.loop:
        analyze_all_symbols()
        return

    while True:
        started_at = datetime.now(timezone.utc).isoformat()
        print(f"\n===== RUN STARTED {started_at} =====")
        analyze_all_symbols()
        print(f"\nSleeping {args.interval} seconds...")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
    
