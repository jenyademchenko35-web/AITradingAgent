"""Read-only Adaptive Market Regime Advisor for AITradingAgent.

The advisor analyzes existing runtime CSV/JSON files and local OHLCV cache
to explain which market regimes produce near-setup signals and trade results.
It never changes live strategy settings or trading decisions.
"""

from __future__ import annotations

import csv
import json
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping


BASE_DIR = Path(__file__).resolve().parent
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
STATS_FILE = BASE_DIR / "agent_v3_stats.json"
TRADES_FILE = BASE_DIR / "trades.csv"
CACHE_DIR = BASE_DIR / "ohlcv_cache"

REPORT_FILE = BASE_DIR / "market_regime_advisor_report.json"
SUMMARY_FILE = BASE_DIR / "market_regime_advisor_summary.txt"
CSV_FILE = BASE_DIR / "market_regime_advisor_by_regime.csv"

REGIMES = (
    "TRENDING_UP",
    "TRENDING_DOWN",
    "RANGING",
    "HIGH_VOLATILITY",
    "LOW_VOLATILITY",
    "UNCERTAIN",
)


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read CSV rows safely."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with path.open("r", newline="", encoding="utf-8") as file:
            return [
                dict(row)
                for row in csv.DictReader(file)
                if row and any(row.values())
            ]
    except (csv.Error, OSError, UnicodeDecodeError):
        return []


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object safely."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a CSV/JSON value to float."""
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return default


def parse_time(value: str) -> datetime | None:
    """Parse timestamps used by the project."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def symbol_to_cache_name(symbol: str) -> str:
    """Return BTC_USDT from BTC/USDT."""
    return symbol.replace("/", "_").upper()


def symbol_short(symbol: str) -> str:
    """Return BTC from BTC/USDT."""
    return (symbol or "N/A").replace("/USDT", "")


def cache_path(symbol: str) -> Path:
    """Return OHLCV cache path for a symbol."""
    return CACHE_DIR / f"{symbol_to_cache_name(symbol)}_1h.csv"


def weighted_score(row: Mapping[str, str]) -> float:
    """Return the strongest already-calculated score for a row."""
    return max(
        safe_float(row.get("weighted_score")),
        safe_float(row.get("long_total")),
        safe_float(row.get("short_total")),
        safe_float(row.get("score")),
    )


def directional_edge(row: Mapping[str, str]) -> float:
    """Return existing directional edge from the debug row."""
    return safe_float(row.get("diff"))


def candidate_side(row: Mapping[str, str]) -> str:
    """Infer the already-calculated candidate side for metrics only."""
    direction = row.get("direction") or ""
    if direction in {"LONG", "SHORT"}:
        return direction
    long_total = safe_float(row.get("long_total"))
    short_total = safe_float(row.get("short_total"))
    if long_total > short_total:
        return "LONG"
    if short_total > long_total:
        return "SHORT"
    return "NEUTRAL"


def side_value(row: Mapping[str, str], prefix: str) -> float:
    """Return engine value on the candidate side."""
    side = candidate_side(row).lower()
    if side in {"long", "short"}:
        return safe_float(row.get(f"{prefix}_{side}"))
    return max(
        safe_float(row.get(f"{prefix}_long")),
        safe_float(row.get(f"{prefix}_short")),
    )


def is_near_setup(row: Mapping[str, str]) -> bool:
    """Return True for near setup using existing decision values."""
    signal = row.get("signal", "")
    if signal in {"WATCH", "SETUP", "HIGH PRIORITY"}:
        return True
    return (
        signal == "NO TRADE"
        and safe_float(row.get("score")) == 0
        and safe_float(row.get("confidence")) >= 60
        and weighted_score(row) >= 18
        and directional_edge(row) >= 7
    )


def ema(values: list[float], period: int) -> list[float]:
    """Calculate EMA series."""
    if not values:
        return []
    multiplier = 2 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append((value - output[-1]) * multiplier + output[-1])
    return output


def atr_pct(candles: list[dict[str, Any]], period: int = 14) -> float:
    """Calculate ATR as percent of close for the latest candle."""
    if len(candles) < 2:
        return 0.0
    ranges: list[float] = []
    for index in range(1, len(candles)):
        high = safe_float(candles[index].get("high"))
        low = safe_float(candles[index].get("low"))
        prev_close = safe_float(candles[index - 1].get("close"))
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    recent = ranges[-period:] if len(ranges) >= period else ranges
    close = safe_float(candles[-1].get("close"))
    return (mean(recent) / close * 100) if recent and close else 0.0


def load_cache(symbol: str) -> list[dict[str, Any]]:
    """Load OHLCV cache for one symbol."""
    rows = read_csv_rows(cache_path(symbol))
    candles: list[dict[str, Any]] = []
    for row in rows:
        timestamp = parse_time(row.get("timestamp", ""))
        if timestamp is None:
            continue
        candles.append(
            {
                "timestamp": timestamp,
                "open": safe_float(row.get("open")),
                "high": safe_float(row.get("high")),
                "low": safe_float(row.get("low")),
                "close": safe_float(row.get("close")),
                "volume": safe_float(row.get("volume")),
            }
        )
    return sorted(candles, key=lambda item: item["timestamp"])


def candles_until(candles: list[dict[str, Any]], timestamp: datetime | None) -> list[dict[str, Any]]:
    """Return candles up to timestamp or all candles when timestamp is missing."""
    if timestamp is None:
        return candles
    selected = [item for item in candles if item["timestamp"] <= timestamp]
    return selected or candles[:1]


def regime_from_cache(
    candles: list[dict[str, Any]],
    timestamp: datetime | None,
) -> tuple[str, dict[str, Any]]:
    """Classify regime from local OHLCV cache."""
    selected = candles_until(candles, timestamp)
    if len(selected) < 60:
        return "UNCERTAIN", {"reason": "недостаточно OHLCV-cache"}

    closes = [safe_float(item.get("close")) for item in selected]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    latest_ema20 = ema20[-1]
    latest_ema50 = ema50[-1]
    previous_ema20 = ema20[-8] if len(ema20) >= 8 else ema20[0]
    slope_pct = ((latest_ema20 - previous_ema20) / previous_ema20 * 100) if previous_ema20 else 0.0
    latest_atr_pct = atr_pct(selected[-60:])

    atr_values = [
        atr_pct(selected[max(0, index - 60):index + 1])
        for index in range(20, len(selected), 4)
    ]
    atr_values = [value for value in atr_values if value > 0]
    low_vol = sorted(atr_values)[int(len(atr_values) * 0.25)] if atr_values else 0.4
    high_vol = sorted(atr_values)[int(len(atr_values) * 0.75)] if atr_values else 1.2

    if latest_atr_pct >= max(high_vol, 1.2):
        regime = "HIGH_VOLATILITY"
    elif latest_atr_pct <= min(low_vol, 0.35):
        regime = "LOW_VOLATILITY"
    elif latest_ema20 > latest_ema50 and slope_pct > 0.05:
        regime = "TRENDING_UP"
    elif latest_ema20 < latest_ema50 and slope_pct < -0.05:
        regime = "TRENDING_DOWN"
    elif abs(latest_ema20 - latest_ema50) / closes[-1] < 0.005:
        regime = "RANGING"
    else:
        regime = "RANGING"

    details = {
        "ema20": round(latest_ema20, 6),
        "ema50": round(latest_ema50, 6),
        "ema20_slope_pct": round(slope_pct, 4),
        "atr_pct": round(latest_atr_pct, 4),
        "low_vol_threshold": round(low_vol, 4),
        "high_vol_threshold": round(high_vol, 4),
    }
    return regime, details


def percentile(values: list[float], ratio: float, default: float) -> float:
    """Return a simple percentile from sorted values."""
    clean = sorted(value for value in values if value > 0)
    if not clean:
        return default
    index = min(len(clean) - 1, max(0, int(len(clean) * ratio)))
    return clean[index]


def build_regime_series(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Precompute regime classification for every cached candle."""
    if len(candles) < 60:
        return []

    closes = [safe_float(item.get("close")) for item in candles]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)

    true_ranges = [0.0]
    for index in range(1, len(candles)):
        high = safe_float(candles[index].get("high"))
        low = safe_float(candles[index].get("low"))
        prev_close = safe_float(candles[index - 1].get("close"))
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    atr_pcts: list[float] = []
    for index, candle in enumerate(candles):
        if index < 14:
            atr_pcts.append(0.0)
            continue
        close = safe_float(candle.get("close"))
        recent = true_ranges[index - 13:index + 1]
        atr_pcts.append((mean(recent) / close * 100) if close else 0.0)

    low_vol = percentile(atr_pcts, 0.25, 0.4)
    high_vol = percentile(atr_pcts, 0.75, 1.2)
    series: list[dict[str, Any]] = []

    for index, candle in enumerate(candles):
        if index < 59:
            continue
        latest_ema20 = ema20[index]
        latest_ema50 = ema50[index]
        previous_ema20 = ema20[max(0, index - 7)]
        close = safe_float(candle.get("close"))
        slope_pct = (
            (latest_ema20 - previous_ema20) / previous_ema20 * 100
            if previous_ema20 else 0.0
        )
        latest_atr_pct = atr_pcts[index]

        if latest_atr_pct >= max(high_vol, 1.2):
            regime = "HIGH_VOLATILITY"
        elif latest_atr_pct <= min(low_vol, 0.35):
            regime = "LOW_VOLATILITY"
        elif latest_ema20 > latest_ema50 and slope_pct > 0.05:
            regime = "TRENDING_UP"
        elif latest_ema20 < latest_ema50 and slope_pct < -0.05:
            regime = "TRENDING_DOWN"
        elif close and abs(latest_ema20 - latest_ema50) / close < 0.005:
            regime = "RANGING"
        else:
            regime = "RANGING"

        series.append(
            {
                "timestamp": candle["timestamp"],
                "regime": regime,
                "details": {
                    "ema20": round(latest_ema20, 6),
                    "ema50": round(latest_ema50, 6),
                    "ema20_slope_pct": round(slope_pct, 4),
                    "atr_pct": round(latest_atr_pct, 4),
                    "low_vol_threshold": round(low_vol, 4),
                    "high_vol_threshold": round(high_vol, 4),
                },
            }
        )
    return series


def regime_from_series(
    series: list[dict[str, Any]],
    timestamp: datetime | None,
) -> tuple[str, dict[str, Any]]:
    """Return regime from a precomputed series."""
    if not series:
        return "UNCERTAIN", {"reason": "нет regime-series"}
    if timestamp is None:
        item = series[-1]
        return item["regime"], item["details"]
    timestamps = [item["timestamp"] for item in series]
    index = bisect_right(timestamps, timestamp) - 1
    if index < 0:
        return "UNCERTAIN", {"reason": "timestamp раньше OHLCV-cache"}
    item = series[index]
    return item["regime"], item["details"]


def regime_from_debug(row: Mapping[str, str]) -> tuple[str, dict[str, Any]]:
    """Fallback regime classification from stored debug text."""
    text = " ".join(
        [
            row.get("trend_reason", ""),
            row.get("risk_reason", ""),
            row.get("summary", ""),
        ]
    ).upper()
    if "НИЗКАЯ ВОЛАТИЛЬНОСТЬ" in text:
        return "LOW_VOLATILITY", {"reason": "debug fallback: low volatility"}
    if "ВЫСОКАЯ ВОЛАТИЛЬНОСТЬ" in text:
        return "HIGH_VOLATILITY", {"reason": "debug fallback: high volatility"}
    bullish = text.count("EMA=BULLISH")
    bearish = text.count("EMA=BEARISH")
    if bullish >= 2 and bullish > bearish:
        return "TRENDING_UP", {"reason": "debug fallback: EMA bullish majority"}
    if bearish >= 2 and bearish > bullish:
        return "TRENDING_DOWN", {"reason": "debug fallback: EMA bearish majority"}
    if "NO CLEAR DIRECTIONAL EDGE" in text:
        return "RANGING", {"reason": "debug fallback: no clear directional edge"}
    return "UNCERTAIN", {"reason": "debug fallback: not enough features"}


class MarketRegimeAdvisor:
    """Read-only market regime advisor."""

    def __init__(self) -> None:
        self.cache_by_symbol: dict[str, list[dict[str, Any]]] = {}
        self.regime_series_by_symbol: dict[str, list[dict[str, Any]]] = {}

    def get_regime(self, row: Mapping[str, str]) -> tuple[str, dict[str, Any]]:
        """Return regime for a decision/trade row."""
        symbol = row.get("symbol", "")
        timestamp = parse_time(row.get("timestamp") or row.get("opened_at", ""))
        if symbol:
            if symbol not in self.cache_by_symbol:
                self.cache_by_symbol[symbol] = load_cache(symbol)
                self.regime_series_by_symbol[symbol] = build_regime_series(
                    self.cache_by_symbol[symbol]
                )
            candles = self.cache_by_symbol[symbol]
            if candles:
                regime, details = regime_from_series(
                    self.regime_series_by_symbol.get(symbol, []),
                    timestamp,
                )
                if regime != "UNCERTAIN":
                    return regime, details
        return regime_from_debug(row)

    def enrich_decisions(self) -> list[dict[str, Any]]:
        """Attach regime and advisor metrics to decision_debug rows."""
        rows = read_csv_rows(DEBUG_FILE)
        enriched: list[dict[str, Any]] = []
        for row in rows:
            regime, details = self.get_regime(row)
            enriched.append(
                {
                    "timestamp": row.get("timestamp", ""),
                    "symbol": row.get("symbol", ""),
                    "decision": row.get("signal", ""),
                    "direction": candidate_side(row),
                    "regime": regime,
                    "confidence": safe_float(row.get("confidence")),
                    "weighted_score": weighted_score(row),
                    "directional_edge": directional_edge(row),
                    "risk": side_value(row, "risk"),
                    "structure_score": side_value(row, "structure"),
                    "momentum_score": side_value(row, "momentum"),
                    "near_setup": is_near_setup(row),
                    "details": details,
                }
            )
        return enriched

    def analyze_regimes(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Calculate decision metrics by regime."""
        by_regime: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_regime[row["regime"]].append(row)

        output: dict[str, Any] = {}
        for regime in REGIMES:
            items = by_regime.get(regime, [])
            output[regime] = {
                "analyses": len(items),
                "no_trade": sum(1 for item in items if item["decision"] == "NO TRADE"),
                "average_confidence": round(avg(item["confidence"] for item in items), 2),
                "average_weighted_score": round(avg(item["weighted_score"] for item in items), 2),
                "average_directional_edge": round(avg(item["directional_edge"] for item in items), 2),
                "average_risk": round(avg(item["risk"] for item in items), 2),
                "average_structure_score": round(avg(item["structure_score"] for item in items), 2),
                "average_momentum_score": round(avg(item["momentum_score"] for item in items), 2),
                "near_setup": sum(1 for item in items if item["near_setup"]),
                "symbols_near_setup": top_symbols(
                    item["symbol"] for item in items if item["near_setup"]
                ),
            }
        return output

    def analyze_trades(self) -> dict[str, Any]:
        """Calculate closed trade performance by regime."""
        rows = [
            row for row in read_csv_rows(TRADES_FILE)
            if (row.get("status") or row.get("result")) in {"WIN", "LOSS"}
        ]
        grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in rows:
            regime, _details = self.get_regime(row)
            grouped[regime].append(row)

        output: dict[str, Any] = {}
        for regime in REGIMES:
            items = grouped.get(regime, [])
            wins = [
                item for item in items
                if (item.get("status") or item.get("result")) == "WIN"
            ]
            losses = [
                item for item in items
                if (item.get("status") or item.get("result")) == "LOSS"
            ]
            pnls = [safe_float(item.get("pnl")) for item in items]
            gross_profit = sum(max(pnl, 0.0) for pnl in pnls)
            gross_loss = sum(abs(min(pnl, 0.0)) for pnl in pnls)
            output[regime] = {
                "trades": len(items),
                "wins": len(wins),
                "losses": len(losses),
                "winrate": round((len(wins) / len(items) * 100) if items else 0.0, 2),
                "profit_factor": round((gross_profit / gross_loss) if gross_loss else 0.0, 2),
                "roi": round(sum(pnls), 2),
                "symbols": top_symbols(item.get("symbol", "") for item in items),
            }
        return output

    def current_regime(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Summarize the latest market regime snapshot."""
        latest_by_symbol: dict[str, dict[str, Any]] = {}
        for row in rows:
            symbol = row.get("symbol", "")
            if not symbol:
                continue
            if symbol not in latest_by_symbol or row["timestamp"] > latest_by_symbol[symbol]["timestamp"]:
                latest_by_symbol[symbol] = row
        counts = Counter(item["regime"] for item in latest_by_symbol.values())
        regime = counts.most_common(1)[0][0] if counts else "UNCERTAIN"
        near_symbols = [
            symbol_short(item["symbol"])
            for item in latest_by_symbol.values()
            if item["near_setup"]
        ]
        return {
            "regime": regime,
            "distribution": dict(counts),
            "symbols_analyzed": len(latest_by_symbol),
            "near_setup_symbols": sorted(near_symbols),
        }

    def recommendations(
        self,
        current: Mapping[str, Any],
        by_regime: Mapping[str, Any],
        trades: Mapping[str, Any],
    ) -> tuple[str, list[str]]:
        """Build advisor-only recommendations in Russian."""
        regime = current.get("regime", "UNCERTAIN")
        near_symbols = current.get("near_setup_symbols", [])
        total_trades = sum(item.get("trades", 0) for item in trades.values())
        recommendations: list[str] = []

        if regime == "HIGH_VOLATILITY":
            recommendations.append("Сегодня высокая волатильность: не ослаблять фильтры.")
        elif regime == "LOW_VOLATILITY":
            recommendations.append("Сегодня низкая волатильность: ждать подтверждения Directional Edge.")
        elif regime == "RANGING":
            recommendations.append("Сегодня рынок во флэте: не ждать сильный breakout без подтверждения.")
        elif regime == "TRENDING_UP":
            recommendations.append("Сегодня TRENDING UP: наблюдать LONG near-setup кандидаты.")
        elif regime == "TRENDING_DOWN":
            recommendations.append("Сегодня TRENDING DOWN: наблюдать SHORT near-setup кандидаты.")
        else:
            recommendations.append("Режим неопределён: не менять стратегию и продолжать наблюдение.")

        if near_symbols:
            recommendations.append(
                "Лучшие символы для наблюдения: " + ", ".join(near_symbols[:5])
            )
        else:
            recommendations.append("Near Setup сейчас нет: действий не требуется.")

        near_by_regime = sorted(
            (
                (name, payload.get("near_setup", 0))
                for name, payload in by_regime.items()
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        if near_by_regime and near_by_regime[0][1] > 0:
            recommendations.append(
                f"Near Setup чаще всего появляется в режиме {near_by_regime[0][0]}."
            )

        if total_trades < 30:
            recommendations.append(
                "По сделкам данных пока мало: выводы по Winrate/PF считать предварительными."
            )

        status = "OBSERVE_ONLY"
        if total_trades < 10:
            status = "INSUFFICIENT_DATA"
        elif regime == "UNCERTAIN":
            status = "NO_ACTION"
        elif near_symbols:
            status = "PROMISING"
        return status, recommendations

    def build_report(self) -> dict[str, Any]:
        """Build the full advisor report."""
        decisions = self.enrich_decisions()
        by_regime = self.analyze_regimes(decisions)
        trade_metrics = self.analyze_trades()
        current = self.current_regime(decisions)
        status, recommendations = self.recommendations(current, by_regime, trade_metrics)

        return {
            "generated_at": utc_now(),
            "status": status,
            "current_market": current,
            "by_regime": by_regime,
            "trades_by_regime": trade_metrics,
            "near_setup_by_regime": {
                regime: {
                    "count": payload.get("near_setup", 0),
                    "symbols": payload.get("symbols_near_setup", {}),
                }
                for regime, payload in by_regime.items()
            },
            "recommendations": recommendations,
            "data_sources": {
                "signals_v3.csv": file_state(SIGNALS_FILE),
                "decision_debug.csv": file_state(DEBUG_FILE),
                "decision_diagnostics.csv": file_state(DIAGNOSTICS_FILE),
                "agent_v3_stats.json": file_state(STATS_FILE),
                "trades.csv": file_state(TRADES_FILE),
                "ohlcv_cache": CACHE_DIR.exists(),
            },
            "agent_stats": read_json(STATS_FILE),
            "notes": [
                "Advisor mode only: no config, strategy or decision changes are applied.",
                "Regime classification uses local OHLCV-cache when available and stored debug features as fallback.",
            ],
        }

    def save_outputs(self, report: Mapping[str, Any]) -> None:
        """Save JSON, TXT and CSV outputs."""
        with REPORT_FILE.open("w", encoding="utf-8") as file:
            json.dump(report, file, indent=2, ensure_ascii=False)
        SUMMARY_FILE.write_text(format_summary(report), encoding="utf-8")
        write_regime_csv(report.get("by_regime", {}), report.get("trades_by_regime", {}))

    def print_report(self, report: Mapping[str, Any]) -> None:
        """Print a compact summary."""
        print(format_summary(report))


def avg(values: Iterable[float]) -> float:
    """Return average or zero."""
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def top_symbols(values: Iterable[str], limit: int = 5) -> dict[str, int]:
    """Return top symbol counts."""
    counts = Counter(value for value in values if value)
    return dict(counts.most_common(limit))


def file_state(path: Path) -> dict[str, Any]:
    """Return lightweight file availability state."""
    return {
        "exists": path.exists(),
        "rows": len(read_csv_rows(path)) if path.suffix == ".csv" else None,
        "size": path.stat().st_size if path.exists() else 0,
    }


def format_summary(report: Mapping[str, Any]) -> str:
    """Format Russian human-readable summary."""
    current = report.get("current_market", {})
    by_regime = report.get("by_regime", {})
    trades = report.get("trades_by_regime", {})
    near = report.get("near_setup_by_regime", {})

    best_near = sorted(
        ((name, payload.get("count", 0)) for name, payload in near.items()),
        key=lambda item: item[1],
        reverse=True,
    )
    best_near_line = (
        f"{best_near[0][0]} ({best_near[0][1]})"
        if best_near and best_near[0][1] > 0
        else "нет данных"
    )

    lines = [
        "Adaptive Market Regime Advisor",
        f"Сгенерировано: {report.get('generated_at', 'N/A')}",
        f"Статус: {report.get('status', 'N/A')}",
        "",
        f"Текущий режим: {current.get('regime', 'UNCERTAIN')}",
        f"Символов в последнем snapshot: {current.get('symbols_analyzed', 0)}",
        "Near Setup сейчас: "
        + (", ".join(current.get("near_setup_symbols", [])) or "нет"),
        f"Near Setup чаще всего: {best_near_line}",
        "",
        "Режимы:",
    ]
    for regime in REGIMES:
        payload = by_regime.get(regime, {})
        trade_payload = trades.get(regime, {})
        lines.append(
            f"- {regime}: analyses={payload.get('analyses', 0)}, "
            f"NO TRADE={payload.get('no_trade', 0)}, "
            f"near={payload.get('near_setup', 0)}, "
            f"WR={trade_payload.get('winrate', 0)}%, "
            f"PF={trade_payload.get('profit_factor', 0)}"
        )
    lines.extend(["", "Рекомендации:"])
    lines.extend(f"- {item}" for item in report.get("recommendations", []))
    lines.extend(
        [
            "",
            f"JSON: {REPORT_FILE.name}",
            f"TXT: {SUMMARY_FILE.name}",
            f"CSV: {CSV_FILE.name}",
        ]
    )
    return "\n".join(lines)


def write_regime_csv(
    by_regime: Mapping[str, Any],
    trades: Mapping[str, Any],
) -> None:
    """Write by-regime CSV report."""
    fieldnames = [
        "regime",
        "analyses",
        "no_trade",
        "average_confidence",
        "average_weighted_score",
        "average_directional_edge",
        "average_risk",
        "average_structure_score",
        "average_momentum_score",
        "near_setup",
        "trades",
        "wins",
        "losses",
        "winrate",
        "profit_factor",
        "roi",
    ]
    with CSV_FILE.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for regime in REGIMES:
            payload = by_regime.get(regime, {})
            trade_payload = trades.get(regime, {})
            writer.writerow(
                {
                    "regime": regime,
                    "analyses": payload.get("analyses", 0),
                    "no_trade": payload.get("no_trade", 0),
                    "average_confidence": payload.get("average_confidence", 0),
                    "average_weighted_score": payload.get("average_weighted_score", 0),
                    "average_directional_edge": payload.get("average_directional_edge", 0),
                    "average_risk": payload.get("average_risk", 0),
                    "average_structure_score": payload.get("average_structure_score", 0),
                    "average_momentum_score": payload.get("average_momentum_score", 0),
                    "near_setup": payload.get("near_setup", 0),
                    "trades": trade_payload.get("trades", 0),
                    "wins": trade_payload.get("wins", 0),
                    "losses": trade_payload.get("losses", 0),
                    "winrate": trade_payload.get("winrate", 0),
                    "profit_factor": trade_payload.get("profit_factor", 0),
                    "roi": trade_payload.get("roi", 0),
                }
            )


def main() -> None:
    """Run advisor from terminal."""
    advisor = MarketRegimeAdvisor()
    report = advisor.build_report()
    advisor.save_outputs(report)
    advisor.print_report(report)


if __name__ == "__main__":
    main()
