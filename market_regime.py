"""Current market regime analytics for AITradingAgent."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping

from config import ATR_HIGH, ATR_LOW
from multi_timeframe_agent_v3 import SYMBOLS, load_market


BASE_DIR = Path(__file__).resolve().parent
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
WEIGHTS_FILE = BASE_DIR / "strategy_weights.json"
JSON_OUTPUT = BASE_DIR / "market_regime_report.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> Dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    return data if isinstance(data, dict) else {}


def read_symbols() -> List[str]:
    """Read symbols from the active v3 universe plus optional weight overrides."""
    weights = read_json(WEIGHTS_FILE)
    symbols = set(SYMBOLS)
    symbols.update(key for key in weights.keys() if key != "global")
    return sorted(symbols)


def classify_regime(market) -> Dict[str, Any]:
    """Classify a market snapshot into trend and volatility regimes."""
    tf1h = market.tf1h
    tf4h = market.tf4h
    tf1d = market.tf1d

    up_confirmed = (
        tf4h.ema20 > tf4h.ema50
        and tf1d.ema20 > tf1d.ema50
        and tf4h.macd > tf4h.macd_signal
        and tf1d.macd > tf1d.macd_signal
    )
    down_confirmed = (
        tf4h.ema20 < tf4h.ema50
        and tf1d.ema20 < tf1d.ema50
        and tf4h.macd < tf4h.macd_signal
        and tf1d.macd < tf1d.macd_signal
    )

    if up_confirmed:
        trend_regime = "Trending Up"
    elif down_confirmed:
        trend_regime = "Trending Down"
    else:
        trend_regime = "Range"

    atr_pct = (tf1h.atr / tf1h.close) * 100 if tf1h.close else 0.0
    if atr_pct > ATR_HIGH:
        volatility_regime = "High Volatility"
    elif atr_pct < ATR_LOW:
        volatility_regime = "Low Volatility"
    else:
        volatility_regime = "Normal Volatility"

    if tf1h.rsi >= 65 or tf4h.rsi >= 65:
        momentum_state = "Overbought"
    elif tf1h.rsi <= 35 or tf4h.rsi <= 35:
        momentum_state = "Oversold"
    else:
        momentum_state = "Neutral"

    return {
        "primary_regime": trend_regime,
        "volatility_regime": volatility_regime,
        "momentum_state": momentum_state,
        "atr_pct_1h": round(atr_pct, 2),
        "snapshot": {
            "tf1h": {
                "close": round(tf1h.close, 4),
                "ema20": round(tf1h.ema20, 4),
                "ema50": round(tf1h.ema50, 4),
                "atr": round(tf1h.atr, 4),
                "rsi": round(tf1h.rsi, 2),
                "macd": round(tf1h.macd, 4),
                "macd_signal": round(tf1h.macd_signal, 4),
            },
            "tf4h": {
                "ema20": round(tf4h.ema20, 4),
                "ema50": round(tf4h.ema50, 4),
                "rsi": round(tf4h.rsi, 2),
                "macd": round(tf4h.macd, 4),
                "macd_signal": round(tf4h.macd_signal, 4),
            },
            "tf1d": {
                "ema20": round(tf1d.ema20, 4),
                "ema50": round(tf1d.ema50, 4),
                "rsi": round(tf1d.rsi, 2),
                "macd": round(tf1d.macd, 4),
                "macd_signal": round(tf1d.macd_signal, 4),
            },
        },
    }


def build_market_regime_report() -> Dict[str, Any]:
    """Build current regime report for all active symbols."""
    symbols = read_symbols()
    per_symbol: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    for symbol in symbols:
        try:
            market = load_market(symbol)
            per_symbol[symbol] = classify_regime(market)
        except Exception as exc:  # pragma: no cover - depends on runtime market access
            errors[symbol] = str(exc)

    trend_counts = Counter(
        payload["primary_regime"]
        for payload in per_symbol.values()
    )
    volatility_counts = Counter(
        payload["volatility_regime"]
        for payload in per_symbol.values()
    )

    return {
        "generated_at": utc_now(),
        "symbols": per_symbol,
        "summary": {
            "trend_distribution": dict(trend_counts),
            "volatility_distribution": dict(volatility_counts),
            "symbols_analyzed": len(per_symbol),
        },
        "errors": errors,
        "notes": [
            "This report classifies the current regime per symbol using existing EMA/ATR/RSI/MACD values.",
            "Historical regime analytics in strategy_research.py are snapshot-based because legacy logs do not store EMA/ATR/RSI/MACD per decision row.",
        ],
    }


def save_report(report: Mapping[str, Any]) -> None:
    with JSON_OUTPUT.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)


def print_summary(report: Mapping[str, Any]) -> None:
    print("Market Regime")
    print(f"Symbols analyzed : {report.get('summary', {}).get('symbols_analyzed', 0)}")
    print(f"JSON report      : {JSON_OUTPUT}")


def main() -> None:
    report = build_market_regime_report()
    save_report(report)
    print_summary(report)


if __name__ == "__main__":
    main()
