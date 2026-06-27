import time
from dataclasses import dataclass
import csv
from datetime import datetime, timezone, timedelta
import argparse
import os
import json
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
import ccxt
import pandas as pd
import asyncio
from notification_manager import (
    load_chat_id,
    format_signal,
    is_duplicate,
    mark_as_sent,
)
from telegram import Bot
from dotenv import load_dotenv
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange

# ==========================
# CONFIG
# ==========================

# ==========================
# STORAGE
# ==========================

SIGNALS_FILE = os.path.join(BASE_DIR, "signals_v3.csv")
STATS_FILE = os.path.join(BASE_DIR, "agent_v3_stats.json")
ACTIVE_SETUPS_FILE = os.path.join(BASE_DIR, "active_setups_v3.json")
SETUP_HISTORY_FILE = os.path.join(BASE_DIR, "setup_history_v3.csv")
DECISION_DEBUG_FILE = os.path.join(BASE_DIR, "decision_debug.csv")

# ==========================
# HELPERS
# ==========================

def log(message: str):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {message}")

def fetch_with_retry(symbol, timeframe):
    delays = [2, 4, 8]
    last_exc = None
    for i, delay in enumerate(delays):
        try:
            return EXCHANGE.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                limit=OHLCV_LIMIT,
            )
        except Exception as e:
            last_exc = e
            if i == len(delays) - 1:
                raise
            time.sleep(delay)
    raise last_exc

def load_active_setups():
    if os.path.exists(ACTIVE_SETUPS_FILE):
        try:
            with open(ACTIVE_SETUPS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_active_setups(data):
    with open(ACTIVE_SETUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def is_setup_active(setup_id):
    setups = load_active_setups()
    ts = setups.get(setup_id)
    if not ts:
        return False
    try:
        last_dt = datetime.fromisoformat(ts)
    except Exception:
        return False
    now = datetime.now(timezone.utc)
    cooldown = timedelta(hours=SETUP_COOLDOWN_HOURS)
    if now < last_dt + cooldown:
        return True
    return False

def mark_setup_active(setup_id):
    setups = load_active_setups()
    setups[setup_id] = datetime.now(timezone.utc).isoformat()
    save_active_setups(setups)

def load_stats():
    if not os.path.exists(STATS_FILE):
        return {
            "runs": 0,
            "analyzed_symbols": 0,
            "api_errors": 0,
            "setup_signals": 0,
            "no_trade_signals": 0,
        }
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "runs": 0,
            "analyzed_symbols": 0,
            "api_errors": 0,
            "setup_signals": 0,
            "no_trade_signals": 0,
        }

def save_stats(stats):
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)

def update_stats(decisions, api_errors):
    stats = load_stats()
    stats["runs"] += 1
    stats["analyzed_symbols"] += len(decisions)
    stats["api_errors"] += api_errors
    stats["setup_signals"] += sum(1 for _, d in decisions if d.signal in ("SETUP", "HIGH PRIORITY"))
    stats["no_trade_signals"] += sum(1 for _, d in decisions if d.signal == "NO TRADE")
    save_stats(stats)

def save_setup_history(symbol, decision):
    # Only save for SETUP or HIGH PRIORITY
    if decision.signal not in ("SETUP", "HIGH PRIORITY"):
        return
    file_exists = os.path.exists(SETUP_HISTORY_FILE)
    needs_header = (not file_exists) or os.path.getsize(SETUP_HISTORY_FILE) == 0
    with open(SETUP_HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if needs_header:
            writer.writerow([
                "timestamp",
                "symbol",
                "direction",
                "signal",
                "score",
                "confidence",
                "quality",
                "long_total",
                "short_total",
                "summary",
            ])
        timestamp = datetime.now(timezone.utc).isoformat()
        writer.writerow([
            timestamp,
            symbol,
            decision.direction,
            decision.signal,
            decision.score,
            decision.confidence,
            decision.quality,
            decision.long_total,
            decision.short_total,
            decision.summary,
        ])

def save_signal(
    symbol: str,
    decision: "DecisionResult",
    trend: "EngineResult",
    structure: "EngineResult",
    momentum: "EngineResult",
    risk: "EngineResult",
):
    file_exists = os.path.exists(SIGNALS_FILE)
    needs_header = (not file_exists) or os.path.getsize(SIGNALS_FILE) == 0
    with open(SIGNALS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if needs_header:
            writer.writerow([
                "timestamp",
                "symbol",
                "direction",
                "signal",
                "score",
                "confidence",
                "quality",
                "trend_long",
                "trend_short",
                "structure_long",
                "structure_short",
                "momentum_long",
                "momentum_short",
                "risk_long",
                "risk_short",
                "long_total",
                "short_total",
                "summary",
            ])
        timestamp = datetime.now(timezone.utc).isoformat()
        writer.writerow([
            timestamp,
            symbol,
            decision.direction,
            decision.signal,
            decision.score,
            decision.confidence,
            decision.quality,
            trend.long,
            trend.short,
            structure.long,
            structure.short,
            momentum.long,
            momentum.short,
            risk.long,
            risk.short,
            decision.long_total,
            decision.short_total,
            decision.summary,
        ])

def save_decision_debug(
    symbol: str,
    decision: "DecisionResult",
    trend: "EngineResult",
    structure: "EngineResult",
    momentum: "EngineResult",
    risk: "EngineResult",
):
    file_exists = os.path.exists(DECISION_DEBUG_FILE)
    needs_header = (not file_exists) or os.path.getsize(DECISION_DEBUG_FILE) == 0

    with open(DECISION_DEBUG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if needs_header:
            writer.writerow([
                "timestamp",
                "symbol",
                "direction",
                "signal",
                "score",
                "confidence",
                "quality",
                "trend_long",
                "trend_short",
                "structure_long",
                "structure_short",
                "momentum_long",
                "momentum_short",
                "risk_long",
                "risk_short",
                "long_total",
                "short_total",
                "diff",
                "winner",
                "trend_reason",
                "structure_reason",
                "momentum_reason",
                "risk_reason",
                "summary",
            ])

        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            symbol,
            decision.direction,
            decision.signal,
            decision.score,
            decision.confidence,
            decision.quality,
            trend.long,
            trend.short,
            structure.long,
            structure.short,
            momentum.long,
            momentum.short,
            risk.long,
            risk.short,
            decision.long_total,
            decision.short_total,
            abs(decision.long_total - decision.short_total),
            decision.direction,
            trend.reason.replace("\n", " | "),
            structure.reason.replace("\n", " | "),
            momentum.reason.replace("\n", " | "),
            risk.reason.replace("\n", " | "),
            decision.summary,
        ])

# ==========================
# SETUP TRACKING CONSTANTS
# ==========================
SETUP_COOLDOWN_HOURS = 6

SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
]

TIMEFRAMES = [
    "1h",
    "4h",
    "1d",
]

OHLCV_LIMIT = 200
RUN_INTERVAL = 900

EXCHANGE = ccxt.bybit(
    {
        "enableRateLimit": True,
        "options": {
            "defaultType": "spot",
        },
    }
)

# ==========================
# MARKET SNAPSHOT
# ==========================

@dataclass
class TFData:
    close: float
    rsi: float
    ema20: float
    ema50: float
    atr: float
    macd: float
    macd_signal: float
    price_position: float
    trend_ema: str
    trend_macd: str


@dataclass
class MarketSnapshot:
    symbol: str
    tf1h: TFData
    tf4h: TFData
    tf1d: TFData


# ==========================
# LOADER
# ==========================

def load_tf(symbol: str, timeframe: str) -> TFData:
    ohlcv = fetch_with_retry(symbol, timeframe)

    df = pd.DataFrame(
        ohlcv,
        columns=[
            "ts",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ],
    )

    df["ema20"] = EMAIndicator(df["close"], window=20).ema_indicator()
    df["ema50"] = EMAIndicator(df["close"], window=50).ema_indicator()

    df["rsi"] = RSIIndicator(df["close"]).rsi()

    macd = MACD(df["close"])

    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()

    df["atr"] = AverageTrueRange(
        df["high"],
        df["low"],
        df["close"],
    ).average_true_range()

    df["high20"] = df["high"].rolling(20).max()
    df["low20"] = df["low"].rolling(20).min()

    range20 = df["high20"] - df["low20"]
    range20 = range20.replace(0, 1e-9)

    df["price_position"] = (
        (df["close"] - df["low20"]) / range20 * 100
    )

    last = df.iloc[-1]

    return TFData(
        close=float(last.close),
        rsi=float(last.rsi),
        ema20=float(last.ema20),
        ema50=float(last.ema50),
        atr=float(last.atr),
        macd=float(last.macd),
        macd_signal=float(last.macd_signal),
        price_position=float(last.price_position),
        trend_ema="BULLISH"
        if last.ema20 > last.ema50
        else "BEARISH",
        trend_macd="BULLISH"
        if last.macd > last.macd_signal
        else "BEARISH",
    )

def load_market(symbol: str) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        tf1h=load_tf(symbol, "1h"),
        tf4h=load_tf(symbol, "4h"),
        tf1d=load_tf(symbol, "1d"),
    )

# ==========================
# ENGINES
# ==========================

@dataclass
class EngineResult:
    long: int
    short: int
    reason: str

@dataclass
class DecisionResult:
    direction: str
    signal: str
    score: int
    long_total: int
    short_total: int
    confidence: float
    quality: str
    summary: str
    explanation: str


class TrendEngine:

    def __init__(self, market: MarketSnapshot):
        self.market = market

    def calculate(self) -> EngineResult:

        long_score = 0
        short_score = 0
        reasons = []

        weights = {
            "1d": 20,
            "4h": 12,
            "1h": 8,
        }

        timeframes = {
            "1d": self.market.tf1d,
            "4h": self.market.tf4h,
            "1h": self.market.tf1h,
        }

        for tf_name, tf in timeframes.items():

            weight = weights[tf_name]

            if tf.trend_ema == "BULLISH":
                long_score += weight
                reasons.append(f"{tf_name}: EMA LONG +{weight}")
            else:
                short_score += weight
                reasons.append(f"{tf_name}: EMA SHORT +{weight}")

            if tf.trend_macd == "BULLISH":
                long_score += weight // 2
                reasons.append(f"{tf_name}: MACD LONG +{weight // 2}")
            else:
                short_score += weight // 2
                reasons.append(f"{tf_name}: MACD SHORT +{weight // 2}")

        # ===== Trend Summary (выполняется ОДИН раз) =====

        reasons.append("")
        reasons.append("=== Trend Summary ===")

        for tf_name, tf in timeframes.items():
            reasons.append(
                f"{tf_name}: "
                f"EMA={tf.trend_ema} | "
                f"MACD={tf.trend_macd} | "
                f"EMA20={tf.ema20:.2f} | "
                f"EMA50={tf.ema50:.2f} | "
                f"MACD={tf.macd:.4f} | "
                f"Signal={tf.macd_signal:.4f}"
            )

        return EngineResult(
            long=long_score,
            short=short_score,
            reason="\n".join(reasons),
        )

# ==========================
# STRUCTURE ENGINE
# ==========================

class StructureEngine:

    def __init__(self, market: MarketSnapshot):
        self.market = market

    def calculate(self) -> EngineResult:

        long_score = 0
        short_score = 0
        reasons = []

        weights = {
            "1d": 10,
            "4h": 8,
            "1h": 7,
        }

        timeframes = {
            "1d": self.market.tf1d,
            "4h": self.market.tf4h,
            "1h": self.market.tf1h,
        }

        for tf_name, tf in timeframes.items():

            weight = weights[tf_name]

            if tf.price_position <= 20:
                long_score += weight
                reasons.append(
                    f"{tf_name}: цена у нижней границы (+{weight} LONG)"
                )

            elif tf.price_position >= 80:
                short_score += weight
                reasons.append(
                    f"{tf_name}: цена у верхней границы (+{weight} SHORT)"
                )

            else:
                reasons.append(f"{tf_name}: середина диапазона")
                

        return EngineResult(
            long=long_score,
            short=short_score,
            reason="\n".join(reasons),
        )


# ==========================
# MOMENTUM ENGINE
# ==========================

class MomentumEngine:

    def __init__(self, market: MarketSnapshot):
        self.market = market

    def calculate(self) -> EngineResult:

        long_score = 0
        short_score = 0
        reasons = []

        weights = {
            "1d": 10,
            "4h": 8,
            "1h": 7,
        }

        timeframes = {
            "1d": self.market.tf1d,
            "4h": self.market.tf4h,
            "1h": self.market.tf1h,
        }

        for tf_name, tf in timeframes.items():
            weight = weights[tf_name]

            # MACD half weight
            macd_half = weight // 2
            if tf.trend_macd == "BULLISH":
                long_score += macd_half
                reasons.append(f"{tf_name}: MACD LONG +{macd_half}")
            else:
                short_score += macd_half
                reasons.append(f"{tf_name}: MACD SHORT +{macd_half}")

            # RSI remaining half
            rsi_half = weight - macd_half
            if tf.rsi < 35:
                long_score += rsi_half
                reasons.append(f"{tf_name}: RSI {tf.rsi:.1f} <35 LONG +{rsi_half}")
            elif tf.rsi > 65:
                short_score += rsi_half
                reasons.append(f"{tf_name}: RSI {tf.rsi:.1f} >65 SHORT +{rsi_half}")
            else:
                reasons.append(f"{tf_name}: RSI {tf.rsi:.1f} neutral")

        return EngineResult(
            long=long_score,
            short=short_score,
            reason="\n".join(reasons),
        )


# ==========================
# RISK ENGINE
# ==========================

class RiskEngine:

    def __init__(self, market: MarketSnapshot):
        self.market = market

    def calculate(self) -> EngineResult:

        long_score = 0
        short_score = 0
        reasons = []

        tf = self.market.tf1h
        pos = tf.price_position
        atr = tf.atr
        price = tf.close

        # ATR volatility effect
        atr_pct = (atr / price) * 100 if price else 0
        if atr_pct > 2:
            long_score -= 5
            short_score -= 5
            reasons.append("Высокая волатильность (-5 LONG, -5 SHORT)")
        elif atr_pct < 1:
            reasons.append("Низкая волатильность")

        if 35 <= pos <= 65:
            reasons.append("Цена далеко от зоны входа")
        elif pos < 35:
            long_score += 15
            reasons.append("Хорошая зона для LONG (+15 LONG)")
        elif pos > 65:
            short_score += 15
            reasons.append("Хорошая зона для SHORT (+15 SHORT)")

        return EngineResult(
            long=long_score,
            short=short_score,
            reason="\n".join(reasons),
        )


# ==========================
# DECISION ENGINE
# ==========================
TREND_WEIGHT = 0.40
STRUCTURE_WEIGHT = 0.25
MOMENTUM_WEIGHT = 0.20
RISK_WEIGHT = 0.15
class DecisionEngine:
   

    @staticmethod
    def calculate(trend: EngineResult, structure: EngineResult, momentum: EngineResult, risk: EngineResult) -> DecisionResult:
        # Weighted totals
        # Trend: 40%, Structure: 25%, Momentum: 20%, Risk: 15%
        trend_long = trend.long * TREND_WEIGHT
        trend_short = trend.short * TREND_WEIGHT

        structure_long = structure.long * STRUCTURE_WEIGHT
        structure_short = structure.short * STRUCTURE_WEIGHT

        momentum_long = momentum.long * MOMENTUM_WEIGHT
        momentum_short = momentum.short * MOMENTUM_WEIGHT

        risk_long = risk.long * RISK_WEIGHT
        risk_short = risk.short * RISK_WEIGHT

        long_total = (
            trend_long
            + structure_long
            + momentum_long
            + risk_long
        )

        short_total = (
            trend_short
            + structure_short
            + momentum_short
            + risk_short
        )

        long_total = int(round(long_total))
        short_total = int(round(short_total))

        abs_diff = abs(long_total - short_total)
        if abs_diff < 10:
            direction = "WAIT"
            signal = "NO TRADE"
            score = max(long_total, short_total)
            summary = "No clear directional edge."
        else:
            if long_total >= short_total:
                direction = "LONG"
                score = long_total
            else:
                direction = "SHORT"
                score = short_total
            if score >= 27:
                signal = "HIGH PRIORITY"
            elif score >= 25:
                signal = "SETUP"
            elif score >= 23:
                signal = "WATCH"
            elif score >= 20:
                signal = "WAIT"
            else:
                signal = "NO TRADE"
            summary = f"{direction} wins by {abs_diff} points"

        confidence = round((max(long_total, short_total) / max(long_total + short_total, 1)) * 100, 1)
        if signal == "HIGH PRIORITY":
            quality = "A"
        elif signal == "SETUP":
            quality = "B"
        elif signal == "WATCH":
            quality = "C"
        elif signal == "WAIT":
            quality = "D"
        else:
            quality = "E"
        explanation = (
            f"Trend {trend.long}/{trend.short} "
            f"({trend_long:.1f}/{trend_short:.1f}) | "

            f"Structure {structure.long}/{structure.short} "
            f"({structure_long:.1f}/{structure_short:.1f}) | "

            f"Momentum {momentum.long}/{momentum.short} "
            f"({momentum_long:.1f}/{momentum_short:.1f}) | "

            f"Risk {risk.long}/{risk.short} "
            f"({risk_long:.1f}/{risk_short:.1f}) | "

            f"Totals {long_total}/{short_total}"
        )
        return DecisionResult(
            direction=direction,
            signal=signal,
            score=score,
            long_total=long_total,
            short_total=short_total,
            confidence=confidence,
            quality=quality,
            summary=summary,
            explanation=explanation,
        )



# ==========================
# Notification Sending
# ==========================

async def send_notification(
        symbol: str,
        decision: DecisionResult,
        market: MarketSnapshot,
    ):
    chat_id = load_chat_id()
    if not chat_id or not BOT_TOKEN:
        return

    text = format_signal(symbol, decision)

    if is_duplicate(text):
        return

    bot = Bot(BOT_TOKEN)
    await bot.send_message(chat_id=chat_id, text=text)
    mark_as_sent(text)

# ==========================
# EXECUTION
# ==========================

def print_engine(title: str, result: EngineResult):
    print("=" * 60)
    print(title)
    print("=" * 60)
    print(f"LONG  : {result.long}")
    print(f"SHORT : {result.short}")
    print(result.reason)
    print()

def analyze_symbol(symbol: str) -> DecisionResult:
    market = load_market(symbol)

    trend = TrendEngine(market).calculate()
    structure = StructureEngine(market).calculate()
    momentum = MomentumEngine(market).calculate()
    risk = RiskEngine(market).calculate()

    decision = DecisionEngine.calculate(
    trend,
    structure,
    momentum,
    risk,
)

    save_signal(symbol, decision, trend, structure, momentum, risk)

    save_decision_debug(
        symbol,
        decision,
        trend,
        structure,
        momentum,
        risk,
    )

    # Setup tracking logic
    setup_id = f"{symbol.replace('/', '_')}_{decision.direction}"
    if decision.signal in ("SETUP", "HIGH PRIORITY"):
        if is_setup_active(setup_id):
            decision.summary += " | COOLDOWN"
            log(f"[{symbol}] Cooldown active for {setup_id}")
            print("   ", decision.explanation)
        else:
            mark_setup_active(setup_id)
            save_setup_history(symbol, decision)

            if (
                decision.quality in ("A", "B")
                and decision.confidence >= 80
            ):
                print(f"📨 Sending notification for {symbol}")
                asyncio.run(send_notification(symbol, decision))
                print("✅ Notification sent")

    # No logging of compact signal or summary here

    return decision

# Main analysis pipeline for one execution cycle
def run_once():
    decisions = []
    api_errors = 0
    for symbol in SYMBOLS:
        t0 = time.time()
        try:
            decision = analyze_symbol(symbol)
            t1 = time.time()
            elapsed = t1 - t0
            # Log the compact line with summary and duration
            log(f"[{symbol}] {decision.direction} | {decision.signal} | Quality={decision.quality} | Score={decision.score} | Confidence={decision.confidence}% | {decision.summary} ({elapsed:.2f}s)")
            print("   ", decision.explanation)
            decisions.append((symbol, decision))
        except Exception as e:
            log(f"[ERROR] {symbol}: {e}")
            api_errors += 1
    update_stats(decisions, api_errors)
    if not decisions:
        print("No symbols analyzed this cycle.")
        return
    # Sort descending by score
    decisions.sort(key=lambda x: x[1].score, reverse=True)
    print("\nBEST SETUP:")
    print("=" * 60)
    symbol, decision = decisions[0]
    print(f"{symbol}: {decision.direction} | {decision.signal} | Quality={decision.quality} | Score={decision.score} | Confidence={decision.confidence}%")
    print("=" * 60)
    print("Ranked summary by score:")
    print("=" * 60)
    for symbol, decision in decisions:
        print(f"{symbol}: {decision.direction} | {decision.signal} | Quality={decision.quality} | Score={decision.score} | Confidence={decision.confidence}%")
    print("=" * 60)

    # Count signals by type
    counts = {
        "HIGH PRIORITY": 0,
        "SETUP": 0,
        "WATCH": 0,
        "WAIT": 0,
        "NO TRADE": 0,
    }
    for _, dec in decisions:
        if dec.signal in counts:
            counts[dec.signal] += 1
        else:
            counts[dec.signal] = 1
    print("Signal counts this cycle:")
    print(f"HIGH PRIORITY: {counts.get('HIGH PRIORITY',0)}")
    print(f"SETUP       : {counts.get('SETUP',0)}")
    print(f"WATCH       : {counts.get('WATCH',0)}")
    print(f"WAIT        : {counts.get('WAIT',0)}")
    print(f"NO TRADE    : {counts.get('NO TRADE',0)}")


# Continuous scheduler
def run_loop(interval_seconds: int):
    try:
        while True:
            cycle_start = time.time()
            print("=" * 60)
            print(f"Cycle started at {datetime.now(timezone.utc).isoformat()}")
            print("=" * 60)
            try:
                run_once()
            except Exception as e:
                print(f"[LOOP ERROR] {e}")
            cycle_end = time.time()
            duration = cycle_end - cycle_start
            print(f"Cycle duration: {duration:.2f} seconds")
            print(f"Sleeping {interval_seconds} seconds...")
            try:
                for _ in range(interval_seconds):
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\nStopping agent...")
                break
    except KeyboardInterrupt:
        print("\nStopping agent...")


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Multi-Timeframe Trading Agent v3")
    parser.add_argument("--loop", action="store_true", help="Run the agent in a loop")
    parser.add_argument("--interval", type=int, default=RUN_INTERVAL, help="Interval between runs in seconds when looping")

    args = parser.parse_args()
    print("=" * 60)
    print(f"Multi-Timeframe Agent v3 started at {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)
    if args.loop:
        run_loop(args.interval)
    else:
        run_once()
        print("Finished.")