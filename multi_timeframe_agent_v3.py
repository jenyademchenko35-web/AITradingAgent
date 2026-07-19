import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import argparse
import os
import json
from config import (
    MIN_CONFIDENCE,
    MIN_EDGE,
    ATR_HIGH,
    ATR_LOW,
    LOG_LEVEL,
    PRICE_ZONE_LOW,
    PRICE_ZONE_HIGH,
    SETUP_COOLDOWN_HOURS,
    RUN_INTERVAL,
)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

WEIGHTS_FILE = os.path.join(BASE_DIR, "strategy_weights.json")

def load_strategy_weights(symbol=None):
    with open(WEIGHTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    LOGGER.csv_read(WEIGHTS_FILE)

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
from explainable_ai import ExplainableAI
from decision_diagnostics import DecisionDiagnostics
from logging_manager import ConsoleOutputManager
from best_candidate_ranker import select_best_candidate
from protective_filter_dry_run import ProtectiveFilterDryRun
from sl_quality_protective_dry_run import SLQualityProtectiveDryRun
from confidence_sl_quality_d_dry_run import ConfidenceSLQualityDDryRun
from portfolio_manager import PortfolioManager
from ada_opportunity_dry_run import ADAOpportunityDryRun
from doge_link_opportunity_dry_run import DogeLinkOpportunityDryRun
from long_rebound_opportunity_dry_run import LongReboundOpportunityDryRun
from relaxed_edge_dry_run import RelaxedEdgeDryRun
from runtime_csv import append_row_atomic_or_locked, ensure_header
from telegram import Bot
from trade_tracker import (
    open_trade,
    get_open_trades,
    close_trade,
)
from trade_close_notifier import TradeCloseNotifier
from dotenv import load_dotenv
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange

LOGGER = ConsoleOutputManager(LOG_LEVEL)
PROTECTIVE_FILTER_DRY_RUN = ProtectiveFilterDryRun()
SL_QUALITY_PROTECTIVE_DRY_RUN = SLQualityProtectiveDryRun()
CONFIDENCE_SL_QUALITY_D_DRY_RUN = ConfidenceSLQualityDDryRun()
PORTFOLIO_MANAGER = PortfolioManager()
ADA_OPPORTUNITY_DRY_RUN = ADAOpportunityDryRun()
DOGE_LINK_OPPORTUNITY_DRY_RUN = DogeLinkOpportunityDryRun()
LONG_REBOUND_OPPORTUNITY_DRY_RUN = LongReboundOpportunityDryRun()
RELAXED_EDGE_DRY_RUN = RelaxedEdgeDryRun()
TRADE_CLOSE_NOTIFIER = TradeCloseNotifier(bot_token=BOT_TOKEN)

# ==========================
# # ==========================
# STRATEGY CONSTANTS
# ==========================


# ==========================


# ==========================
# STORAGE
# ==========================

SIGNALS_FILE = os.path.join(BASE_DIR, "signals_v3.csv")
STATS_FILE = os.path.join(BASE_DIR, "agent_v3_stats.json")
ACTIVE_SETUPS_FILE = os.path.join(BASE_DIR, "active_setups_v3.json")
SETUP_HISTORY_FILE = os.path.join(BASE_DIR, "setup_history_v3.csv")
DECISION_DEBUG_FILE = os.path.join(BASE_DIR, "decision_debug.csv")

SETUP_HISTORY_FIELDS = [
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
]

SIGNAL_FIELDS = [
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
]

DECISION_DEBUG_FIELDS = [
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
]

def ensure_csv_schema(file_path: str, fieldnames: list[str]) -> None:
    """Validate a runtime CSV without scanning its data rows."""
    result = ensure_header(file_path, fieldnames)
    if result.migrated:
        LOGGER.schema_migrated(file_path)
        LOGGER.timestamped(
            f"CSV schema migration rows={result.rows_migrated} "
            f"backup={result.backup_path}",
            minimum="NORMAL",
        )
        LOGGER.csv_write(file_path)

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

            LOGGER.api_retry(symbol, timeframe, i + 1, len(delays), e)

            recreate_exchange()
            time.sleep(delay)

    if cache_key in OHLCV_CACHE:
        LOGGER.cache_used(symbol, timeframe)
        return OHLCV_CACHE[cache_key]

    raise last_exc

def load_active_setups():
    if os.path.exists(ACTIVE_SETUPS_FILE):
        try:
            with open(ACTIVE_SETUPS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            LOGGER.csv_read(ACTIVE_SETUPS_FILE)
            return data
        except Exception:
            return {}
    return {}

def save_active_setups(data):
    with open(ACTIVE_SETUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    LOGGER.csv_write(ACTIVE_SETUPS_FILE)

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
            data = json.load(f)
        LOGGER.csv_read(STATS_FILE)
        return data
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
    LOGGER.csv_write(STATS_FILE)

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
    append_row_atomic_or_locked(
        SETUP_HISTORY_FILE,
        SETUP_HISTORY_FIELDS,
        [
            datetime.now(timezone.utc).isoformat(),
            symbol,
            decision.direction,
            decision.signal,
            decision.score,
            decision.confidence,
            decision.quality,
            decision.long_total,
            decision.short_total,
            decision.summary,
        ],
    )
    LOGGER.csv_write(SETUP_HISTORY_FILE)

def save_signal(
    symbol: str,
    decision: "DecisionResult",
    trend: "EngineResult",
    structure: "EngineResult",
    momentum: "EngineResult",
    risk: "EngineResult",
):
    append_row_atomic_or_locked(
        SIGNALS_FILE,
        SIGNAL_FIELDS,
        [
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
            decision.summary,
        ],
    )
    LOGGER.csv_write(SIGNALS_FILE)

def save_decision_debug(
    symbol: str,
    decision: "DecisionResult",
    trend: "EngineResult",
    structure: "EngineResult",
    momentum: "EngineResult",
    risk: "EngineResult",
):
    append_row_atomic_or_locked(
        DECISION_DEBUG_FILE,
        DECISION_DEBUG_FIELDS,
        [
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
        ],
    )
    LOGGER.csv_write(DECISION_DEBUG_FILE)

# ==========================
# SETUP TRACKING CONSTANTS
# ==========================
SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "XRP/USDT",
    "LINK/USDT",
    "ADA/USDT",
    "AVAX/USDT",
    "DOGE/USDT",
]

TIMEFRAMES = [
    "1h",
    "4h",
    "1d",
]

OHLCV_LIMIT = 200

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
    high20: float
    low20: float
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
        high20=float(last.high20),
        low20=float(last.low20),
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


def send_trade_close_notification(
    trade: dict,
    result: str,
    exit_price=None,
    pnl=None,
) -> None:
    """Send a one-time Telegram notification after a trade is closed."""
    try:
        outcome = asyncio.run(
            TRADE_CLOSE_NOTIFIER.notify_closed_trade(
                trade_hint=trade,
                result=result,
                exit_price=exit_price,
                pnl=pnl,
            )
        )
    except Exception as exc:
        LOGGER.timestamped(
            f"Trade close notification error: {exc}",
            minimum="NORMAL",
        )
        return

    if outcome.get("sent"):
        LOGGER.timestamped(
            f"Trade close notification sent for {trade.get('symbol', 'N/A')}",
            minimum="NORMAL",
        )
        return

    reason = outcome.get("reason", "unknown")
    minimum = "DEBUG" if reason in {"duplicate", "missing_chat_id"} else "NORMAL"
    LOGGER.timestamped(
        (
            f"Trade close notification skipped for "
            f"{trade.get('symbol', 'N/A')}: {reason}"
        ),
        minimum=minimum,
    )

# ==========================
# EXECUTION
# ==========================

def analyze_market(market: MarketSnapshot, symbol: str):
    timings = {}

    t0 = time.time()
    trend = TrendEngine(market).calculate()
    timings["trend"] = time.time() - t0

    t0 = time.time()
    structure = StructureEngine(market).calculate()
    timings["structure"] = time.time() - t0

    t0 = time.time()
    momentum = MomentumEngine(market).calculate()
    timings["momentum"] = time.time() - t0

    t0 = time.time()
    risk = RiskEngine(market).calculate()
    timings["risk"] = time.time() - t0

    weights = load_strategy_weights(symbol)

    t0 = time.time()
    decision = DecisionEngine.calculate(
        trend,
        structure,
        momentum,
        risk,
        weights,
    )
    timings["decision"] = time.time() - t0
    LOGGER.engine_timings(symbol, timings)

    return decision, trend, structure, momentum, risk, weights

def analyze_symbol(symbol: str, cycle_id: str = ""):
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

    decision, trend, structure, momentum, risk, weights = analyze_market(
        market=market,
        symbol=symbol,
    )
    decision_timestamp = datetime.now(timezone.utc).isoformat()
    decision.timestamp = decision_timestamp
    decision.decision_timestamp = decision_timestamp
    decision.cycle_id = cycle_id or decision_timestamp
    decision.stage = "RAW_DECISION"
    decision.raw_signal_status = decision.signal
    decision.final_filter_status = "PENDING"
    decision.execution_status = "NO_TRADE"
    decision.veto_reasons = []
    decision.failed_filters = []
    save_signal(symbol, decision, trend, structure, momentum, risk)

    save_decision_debug(
        symbol,
        decision,
        trend,
        structure,
        momentum,
        risk,
    )

    xai = ExplainableAI(symbol=symbol, auto_log=False)
    xai_report = xai.explain(
        decision,
        trend,
        structure,
        momentum,
        risk,
    )

    diagnostics = DecisionDiagnostics(
        symbol=symbol,
        weights=weights,
        auto_log=False,
    )
    diagnostics_report = diagnostics.analyze(
        decision,
        trend,
        structure,
        momentum,
        risk,
    )
    reports_logged = False

    def log_final_status() -> None:
        nonlocal reports_logged
        if reports_logged:
            return
        diagnostics.sync_report_status(diagnostics_report, decision)
        for key in (
            "raw_signal_status",
            "final_filter_status",
            "execution_status",
            "veto_reasons",
            "failed_filters",
            "cycle_id",
            "decision_timestamp",
            "stage",
        ):
            xai_report[key] = getattr(decision, key, xai_report.get(key, ""))
        for label, writer, report in (
            ("XAI", xai.log_report, xai_report),
            ("Diagnostics", diagnostics.log, diagnostics_report),
        ):
            try:
                writer(report)
            except (OSError, RuntimeError, ValueError) as exc:
                LOGGER.timestamped(
                    f"[{symbol}] {label} status log error: {exc}",
                    minimum="NORMAL",
                )
        LOGGER.analysis_reports(
            xai.format_report(xai_report),
            diagnostics.format_report(diagnostics_report),
        )
        reports_logged = True
    PROTECTIVE_FILTER_DRY_RUN.evaluate(
        symbol=symbol,
        decision=decision,
        market=market,
        diagnostics_report=diagnostics_report,
    )
    SL_QUALITY_PROTECTIVE_DRY_RUN.evaluate(
        symbol=symbol,
        decision=decision,
        market=market,
    )
    CONFIDENCE_SL_QUALITY_D_DRY_RUN.evaluate(
        symbol=symbol,
        decision=decision,
        market=market,
    )
    ADA_OPPORTUNITY_DRY_RUN.evaluate(
        symbol=symbol,
        decision=decision,
    )
    DOGE_LINK_OPPORTUNITY_DRY_RUN.evaluate(
        symbol=symbol,
        decision=decision,
    )
    LONG_REBOUND_OPPORTUNITY_DRY_RUN.evaluate(
        symbol=symbol,
        decision=decision,
        risk=risk,
        trend=trend,
    )
    RELAXED_EDGE_DRY_RUN.evaluate(
        symbol=symbol,
        decision=decision,
    )
    # Setup tracking logic
    setup_id = f"{symbol.replace('/', '_')}_{decision.direction}"
    qualifies_for_execution = (
            decision.signal in ("SETUP", "HIGH PRIORITY")
            and decision.quality in ("A", "B")
            and decision.confidence >= MIN_CONFIDENCE
            and abs(decision.long_total - decision.short_total) >= MIN_EDGE
    )
    if qualifies_for_execution:
        if is_setup_active(setup_id):
            decision.summary += " | COOLDOWN"
            diagnostics.set_execution_status(
                decision,
                "BLOCKED_COOLDOWN",
                f"Cooldown active for {setup_id}",
            )
            LOGGER.cooldown_active(symbol, setup_id)
        else:
            diagnostics.set_execution_status(decision, "ELIGIBLE")
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
                diagnostics.set_execution_status(
                    decision,
                    "ALREADY_OPEN",
                    f"Open trade already exists for {symbol}",
                )
                LOGGER.open_trade_exists(symbol)
                log_final_status()
                return decision, market

            if decision.direction == "LONG" and not higher_tf_bull:
                diagnostics.set_execution_status(
                    decision,
                    "BLOCKED_HIGHER_TF",
                    "LONG rejected by higher timeframe trend filter",
                )
                LOGGER.higher_tf_rejected(symbol, "LONG")
                log_final_status()
                return decision, market

            if decision.direction == "SHORT" and not higher_tf_bear:
                diagnostics.set_execution_status(
                    decision,
                    "BLOCKED_HIGHER_TF",
                    "SHORT rejected by higher timeframe trend filter",
                )
                LOGGER.higher_tf_rejected(symbol, "SHORT")
                log_final_status()
                return decision, market

            portfolio_evaluation = PORTFOLIO_MANAGER.can_open_trade({
                "symbol": symbol,
                "direction": decision.direction,
                "decision": decision.signal,
                "score": decision.score,
                "confidence": decision.confidence,
                "entry": entry,
                "stop_loss": stop_loss,
            })
            if portfolio_evaluation["status"] == "BLOCK":
                reasons = portfolio_evaluation["reasons"]
                diagnostics.set_execution_status(
                    decision,
                    "BLOCKED_PORTFOLIO",
                    " | ".join(reasons),
                )
                LOGGER.timestamped(
                    f"[Portfolio] {symbol} BLOCK: {' | '.join(reasons)}",
                    minimum="NORMAL",
                )
                log_final_status()
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
                LOGGER.notification_sending(symbol)
                asyncio.run(
                    send_notification(
                        symbol,
                        decision,
                        market,
                    )
                )
                LOGGER.notification_sent()
    elif decision.raw_signal_status in ("SETUP", "HIGH PRIORITY"):
        diagnostics.set_execution_status(
            decision,
            "BLOCKED_FILTERS",
            "Mandatory execution thresholds were not satisfied",
        )
    else:
        diagnostics.set_execution_status(decision, "NO_TRADE")

    # No logging of compact signal or summary here
    log_final_status()
    return decision, market

def update_open_trades(current_prices):
    trades = get_open_trades()

    for trade in trades:

        symbol = trade["symbol"]

        LOGGER.checking_trade(symbol)

        if symbol not in current_prices:
            continue

        price = current_prices[symbol]

        direction = trade["direction"]

        sl = float(trade["stop_loss"])
        tp = float(trade["take_profit"])

        LOGGER.trade_snapshot(symbol, direction, price, sl, tp)

        if direction == "LONG":

            if price <= sl:
                pnl = price - float(trade["entry"])
                close_trade(symbol, "LOSS")
                LOGGER.trade_result(symbol, "LOSS")
                send_trade_close_notification(
                    trade,
                    "LOSS",
                    exit_price=price,
                    pnl=round(pnl, 2),
                )

            elif price >= tp:
                pnl = abs(price - float(trade["entry"]))
                close_trade(symbol, "WIN", exit_price=price, pnl=round(pnl, 2))
                LOGGER.trade_result(symbol, "WIN")
                send_trade_close_notification(
                    trade,
                    "WIN",
                    exit_price=price,
                    pnl=round(pnl, 2),
                )

        else:

            if price >= sl:
                pnl = -abs(price - float(trade["entry"]))
                close_trade(symbol, "LOSS", exit_price=price, pnl=round(pnl, 2))
                LOGGER.trade_result(symbol, "LOSS")
                send_trade_close_notification(
                    trade,
                    "LOSS",
                    exit_price=price,
                    pnl=round(pnl, 2),
                )

            elif price <= tp:
                pnl = abs(float(trade["entry"]) - price)
                close_trade(symbol, "WIN", exit_price=price, pnl=round(pnl, 2))
                LOGGER.trade_result(symbol, "WIN")
                send_trade_close_notification(
                    trade,
                    "WIN",
                    exit_price=price,
                    pnl=round(pnl, 2),
                )
# Main analysis pipeline for one execution cycle
def run_once():
    decisions = []
    api_errors = 0
    current_prices = {}
    analysis_contexts = {}
    cycle_id = datetime.now(timezone.utc).isoformat()
    for symbol in SYMBOLS:
        analysis_started_at = datetime.now(timezone.utc)
        t0 = time.time()
        try:
            decision, market = analyze_symbol(symbol, cycle_id=cycle_id)
            analysis_finished_at = datetime.now(timezone.utc)
            analysis_contexts[symbol] = (analysis_started_at, analysis_finished_at)
            current_prices[symbol] = market.tf1h.close
            t1 = time.time()
            elapsed = t1 - t0
            LOGGER.symbol_summary(symbol, decision, elapsed)
            decisions.append((symbol, decision))
        except Exception as e:
            LOGGER.symbol_error(symbol, e)
            api_errors += 1
    update_stats(decisions, api_errors)
    if not decisions:
        LOGGER.no_symbols_analyzed()
        return
    best_candidate = select_best_candidate(decisions)
    if best_candidate:
        decision_by_symbol = {item_symbol: item_decision for item_symbol, item_decision in decisions}
        analysis_started_at, analysis_finished_at = analysis_contexts[best_candidate.symbol]
        LOGGER.best_setup(
            best_candidate.symbol,
            decision_by_symbol[best_candidate.symbol],
            analysis_started_at.isoformat(),
            analysis_finished_at.isoformat(),
        )
    LOGGER.ranked_summary(decisions)

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
    LOGGER.signal_counts(counts)

    update_open_trades(current_prices)


# Continuous scheduler
def run_loop(interval_seconds: int):
    try:
        while True:
            cycle_start = time.time()
            LOGGER.cycle_started(datetime.now(timezone.utc).isoformat())
            try:
                run_once()
            except Exception as e:
                LOGGER.loop_error(e)
            cycle_end = time.time()
            duration = cycle_end - cycle_start
            LOGGER.cycle_finished(duration)
            LOGGER.sleeping(interval_seconds)
            try:
                for _ in range(interval_seconds):
                    time.sleep(1)
            except KeyboardInterrupt:
                LOGGER.stopping()
                break
    except KeyboardInterrupt:
        LOGGER.stopping()


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Multi-Timeframe Trading Agent v3")
    parser.add_argument("--loop", action="store_true", help="Run the agent in a loop")
    parser.add_argument("--interval", type=int, default=RUN_INTERVAL, help="Interval between runs in seconds when looping")

    args = parser.parse_args()
    LOGGER.startup(datetime.now(timezone.utc).isoformat())
    if args.loop:
        run_loop(args.interval)
    else:
        run_once()
        LOGGER.finished()
