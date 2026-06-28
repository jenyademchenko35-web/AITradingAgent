import time
from dataclasses import dataclass
import csv
from datetime import datetime, timezone, timedelta
import argparse
import os
import json
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

WEIGHTS_FILE = os.path.join(BASE_DIR, "strategy_weights.json")

def load_strategy_weights(symbol=None):
    with open(WEIGHTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    if symbol and symbol in data:
        return data[symbol]
    return data["global"]
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
from trade_tracker import (
    open_trade,
    get_open_trades,
    close_trade,
)
from dotenv import load_dotenv
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange

# ==========================
# # ==========================
# STRATEGY CONSTANTS
# ==========================

MIN_CONFIDENCE = 80
MIN_EDGE = 15

ATR_HIGH = 2.0
ATR_LOW = 1.0

PRICE_ZONE_LOW = 35
PRICE_ZONE_HIGH = 65

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
    cache_key = (symbol, timeframe)

    delays = [2, 5, 10]
    last_exc = None

    for i, delay in enumerate(delays):
        try:
            data = EXCHANGE.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                limit=OHLCV_LIMIT,
            )

            OHLCV_CACHE[cache_key] = data
            return data

        except Exception as e:
            last_exc = e

            log(
                f"API error {symbol} {timeframe} "
                f"({i + 1}/{len(delays)}): {e}"
            )

            recreate_exchange()
            time.sleep(delay)

    if cache_key in OHLCV_CACHE:
        log(f"Using cached OHLCV for {symbol} {timeframe}")
        return OHLCV_CACHE[cache_key]

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
        "timeout": 30000,
        "options": {
            "defaultType": "spot",
        },
    }
)

def recreate_exchange():
    global EXCHANGE

    EXCHANGE = ccxt.bybit(
        {
            "enableRateLimit": True,
            "timeout": 30000,
            "options": {
                "defaultType": "spot",
            },
        }
    )
OHLCV_CACHE = {}

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

def build_tf(df: pd.DataFrame) -> TFData:
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
    return build_tf(df)

def load_market(symbol: str) -> MarketSnapshot:
    return build_market_snapshot(
        symbol=symbol,
        tf1h_df=pd.DataFrame(
            fetch_with_retry(symbol, "1h"),
            columns=["ts", "open", "high", "low", "close", "volume"],
        ),
        tf4h_df=pd.DataFrame(
            fetch_with_retry(symbol, "4h"),
            columns=["ts", "open", "high", "low", "close", "volume"],
        ),
        tf1d_df=pd.DataFrame(
            fetch_with_retry(symbol, "1d"),
            columns=["ts", "open", "high", "low", "close", "volume"],
        ),
    )

def build_market_snapshot(
    symbol: str,
    tf1h_df: pd.DataFrame,
    tf4h_df: pd.DataFrame,
    tf1d_df: pd.DataFrame,
) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        tf1h=build_tf(tf1h_df),
        tf4h=build_tf(tf4h_df),
        tf1d=build_tf(tf1d_df),
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

            # Цена относительно EMA20
            if tf.close > tf.ema20:
                long_score += 1
                reasons.append(f"{tf_name}: цена выше EMA20 (+1 LONG)")
            else:
                short_score += 1
                reasons.append(f"{tf_name}: цена ниже EMA20 (+1 SHORT)")

            # Цена относительно EMA50
            if tf.close > tf.ema50:
                long_score += 1
                reasons.append(f"{tf_name}: цена выше EMA50 (+1 LONG)")
            else:
                short_score += 1
                reasons.append(f"{tf_name}: цена ниже EMA50 (+1 SHORT)")

            # Взаимное расположение EMA
            if tf.ema20 > tf.ema50:
                long_score += 2
                reasons.append(f"{tf_name}: EMA20 > EMA50 (+2 LONG)")
            else:
                short_score += 2
                reasons.append(f"{tf_name}: EMA20 < EMA50 (+2 SHORT)")

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
        if atr_pct > ATR_HIGH:
            long_score -= 5
            short_score -= 5
            reasons.append("Высокая волатильность (-5 LONG, -5 SHORT)")
        elif atr_pct < ATR_LOW:
            reasons.append("Низкая волатильность")

        if PRICE_ZONE_LOW <= pos <= PRICE_ZONE_HIGH:
            long_score -= 5
            short_score -= 5
            reasons.append("Цена в середине диапазона (-5 LONG, -5 SHORT)")
        elif pos < PRICE_ZONE_LOW:
            long_score += 20
            reasons.append("Хорошая зона для LONG (+20 LONG)")
        elif pos > PRICE_ZONE_HIGH:
            short_score += 20
            reasons.append("Хорошая зона для SHORT (+20 SHORT)")

        return EngineResult(
            long=long_score,
            short=short_score,
            reason="\n".join(reasons),
        )


class DecisionEngine:
   
    @staticmethod
    def calculate(trend: EngineResult, structure: EngineResult, momentum: EngineResult, risk: EngineResult, weights) -> DecisionResult:
        trend_weight = weights["trend"]
        structure_weight = weights["structure"]
        momentum_weight = weights["momentum"]
        risk_weight = weights["risk"]

        trend_long = trend.long * trend_weight
        trend_short = trend.short * trend_weight

        structure_long = structure.long * structure_weight
        structure_short = structure.short * structure_weight

        momentum_long = momentum.long * momentum_weight
        momentum_short = momentum.short * momentum_weight

        risk_long = risk.long * risk_weight
        risk_short = risk.short * risk_weight

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

        diff = abs(long_total - short_total)

        confidence = min(
            100,
            round(50 + diff * 3, 1),
        )
        if diff < MIN_EDGE:
            direction = "NEUTRAL"
            signal = "NO TRADE"
            score = 0
            summary = "No clear directional edge."
        else:
            if long_total >= short_total:
                direction = "LONG"
                score = long_total
            else:
                direction = "SHORT"
                score = short_total
            if score >= 27 and confidence >= 90:
                signal = "HIGH PRIORITY"

            elif score >= 25 and confidence >= 80:
                signal = "SETUP"

            elif score >= 23 and confidence >= 70:
                signal = "WATCH"

            elif score >= 20:
                signal = "WAIT"

            else:
                signal = "NO TRADE"
            summary = f"{direction} wins by {diff} points"

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

    text = format_signal(symbol, decision, market)

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

def analyze_market(market: MarketSnapshot, symbol: str):
    trend = TrendEngine(market).calculate()
    structure = StructureEngine(market).calculate()
    momentum = MomentumEngine(market).calculate()
    risk = RiskEngine(market).calculate()

    weights = load_strategy_weights(symbol)

    decision = DecisionEngine.calculate(
        trend,
        structure,
        momentum,
        risk,
        weights,
    )

    return decision, trend, structure, momentum, risk

def analyze_symbol(symbol: str):
    market = load_market(symbol)
    # Higher timeframe trend filter
    higher_tf_bull = (
        market.tf4h.ema20 > market.tf4h.ema50
        and market.tf1d.ema20 > market.tf1d.ema50
    )

    higher_tf_bear = (
        market.tf4h.ema20 < market.tf4h.ema50
        and market.tf1d.ema20 < market.tf1d.ema50
    )

    decision, trend, structure, momentum, risk = analyze_market(
    market=market,
    symbol=symbol,
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
    if (
            decision.signal in ("SETUP", "HIGH PRIORITY")
            and decision.quality in ("A", "B")
            and decision.confidence >= MIN_CONFIDENCE
            and abs(decision.long_total - decision.short_total) >= MIN_EDGE
        ):
        if is_setup_active(setup_id):
            decision.summary += " | COOLDOWN"
            log(f"[{symbol}] Cooldown active for {setup_id}")
            print("   ", decision.explanation)
        else:
            mark_setup_active(setup_id)
            entry = market.tf1h.close

            if decision.direction == "LONG":
                stop_loss = entry - market.tf1h.atr
                take_profit = entry + market.tf1h.atr * 2
            else:
                stop_loss = entry + market.tf1h.atr
                take_profit = entry - market.tf1h.atr * 2

            existing_trade = next(
                (t for t in get_open_trades() if t["symbol"] == symbol),
                None,
            )

            if existing_trade:
                log(f"[{symbol}] Open trade already exists, skipping.")
                return decision, market

            if decision.direction == "LONG" and not higher_tf_bull:
                log(f"[{symbol}] LONG rejected by higher timeframe trend filter.")
                return decision, market

            if decision.direction == "SHORT" and not higher_tf_bear:
                log(f"[{symbol}] SHORT rejected by higher timeframe trend filter.")
                return decision, market

            open_trade(
                symbol=symbol,
                direction=decision.direction,
                entry=entry,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            save_setup_history(symbol, decision)

            if (
                decision.quality in ("A", "B")
                and decision.confidence >= 75
            ):
                print(f"📨 Sending notification for {symbol}")
                asyncio.run(
                    send_notification(
                        symbol,
                        decision,
                        market,
                    )
                )
                print("✅ Notification sent")

    # No logging of compact signal or summary here

    return decision, market

def update_open_trades(current_prices):
    trades = get_open_trades()

    for trade in trades:

        symbol = trade["symbol"]

        print(f"Checking trade: {symbol}")

        if symbol not in current_prices:
            continue

        price = current_prices[symbol]

        direction = trade["direction"]

        sl = float(trade["stop_loss"])
        tp = float(trade["take_profit"])

        print(f"🔍 {symbol} | {direction} | Price={price:.2f} | SL={sl:.2f} | TP={tp:.2f}")

        if direction == "LONG":

            if price <= sl:
                close_trade(symbol, "LOSS")
                print(f"❌ {symbol} -> LOSS")

            elif price >= tp:
                pnl = abs(price - float(trade["entry"]))
                close_trade(symbol, "WIN", exit_price=price, pnl=round(pnl, 2))
                print(f"✅ {symbol} -> WIN")

        else:

            if price >= sl:
                pnl = -abs(price - float(trade["entry"]))
                close_trade(symbol, "LOSS", exit_price=price, pnl=round(pnl, 2))
                print(f"❌ {symbol} -> LOSS")

            elif price <= tp:
                pnl = abs(float(trade["entry"]) - price)
                close_trade(symbol, "WIN", exit_price=price, pnl=round(pnl, 2))
                print(f"✅ {symbol} -> WIN")
# Main analysis pipeline for one execution cycle
def run_once():
    decisions = []
    api_errors = 0
    current_prices = {}
    for symbol in SYMBOLS:
        t0 = time.time()
        try:
            decision, market = analyze_symbol(symbol)
            current_prices[symbol] = market.tf1h.close
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

    update_open_trades(current_prices)


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