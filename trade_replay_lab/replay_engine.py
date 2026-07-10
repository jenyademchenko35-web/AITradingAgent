"""Core orchestration for read-only Trade Replay Lab."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping

from market_intelligence_utils import parse_time, safe_float, symbol_full, trade_result
from trade_replay_lab.replay_loader import ReplayLoader
from trade_replay_lab.replay_memory import ReplayMemory
from trade_replay_lab.replay_metrics import (
    baseline_r,
    percent,
    replay_confidence,
    rounded_mean,
)
from trade_replay_lab.replay_patterns import (
    aggregate_patterns,
    build_verdict,
    hypothesis_summary,
)
from trade_replay_lab.replay_scenarios import ReplayScenarioAnalyzer


TREND_PATTERN = re.compile(
    r"(?i)\b(1h|4h|1d)\s*:\s*[^|]*?\b(LONG|SHORT)\b\s*\+?([0-9.]+)"
)


class TradeReplayEngine:
    """Explain each closed trade and test isolated historical alternatives."""

    def __init__(self, loader: ReplayLoader | None = None) -> None:
        self.loader = loader or ReplayLoader()
        self.scenarios = ReplayScenarioAnalyzer(self.loader.ohlcv)

    def build_report(self) -> dict[str, Any]:
        """Build a complete replay report without modifying live artifacts."""
        replays: list[dict[str, Any]] = []
        for index, trade in enumerate(self.loader.closed_trades):
            replay = self.analyze_trade(trade, index, replays)
            replays.append(replay)

        patterns = aggregate_patterns(replays)
        hypotheses = hypothesis_summary(replays)
        wins = sum(1 for row in replays if row.get("result") == "WIN")
        losses = sum(1 for row in replays if row.get("result") == "LOSS")
        ohlcv_count = sum(1 for row in replays if row.get("ohlcv_available"))
        decision_count = sum(1 for row in replays if row.get("decision_available"))
        loss_reasons = Counter(
            row.get("verdict", {}).get("main_reason_label", "")
            for row in replays
            if row.get("result") == "LOSS"
        )
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": self.status(replays, ohlcv_count),
            "mode": "read-only post-trade replay",
            "sample": {
                "closed_trades": len(replays),
                "wins": wins,
                "losses": losses,
                "winrate": percent(wins, len(replays)),
                "decision_coverage": percent(decision_count, len(replays)),
                "ohlcv_coverage": percent(ohlcv_count, len(replays)),
                "minimum_for_strategy_conclusion": 30,
                "enough_for_strategy_conclusion": len(replays) >= 30,
            },
            "summary": {
                "average_improvement_score": rounded_mean(
                    safe_float(row.get("improvement_score")) for row in replays
                ),
                "top_loss_reasons": [
                    {"reason": reason, "count": count}
                    for reason, count in loss_reasons.most_common(10)
                    if reason
                ],
                **hypotheses,
            },
            "trades": replays,
            "patterns": patterns,
            "source_status": self.loader.source_status,
            "warnings": self.loader.warnings,
            "limitations": [
                "OHLCV cache содержит timeframe 1h; +5m/+15m/+30m и minute entry variants не моделируются.",
                "Heatmap используется только при близком времени snapshot; текущий snapshot не переносится в прошлое.",
                "Если exit_price отсутствует, baseline R оценивается по записанному WIN/LOSS и SL/TP.",
                "Intrabar порядок неизвестен: при касании SL и TP в одной 1h свече применяется консервативный SL-first.",
            ],
            "recommendation": (
                "Статистика пока недостаточна для изменения LIVE. Продолжать replay-наблюдение."
                if len(replays) < 30
                else "Использовать лидирующие варианты только как гипотезы для отдельного backtest/dry-run."
            ),
            "restrictions": [
                "DecisionEngine, config.py и стратегия не менялись.",
                "Entry/Exit, SL/TP/RR и PortfolioManager не менялись.",
                "Replay Lab не открывает и не закрывает сделки.",
                "Replay Lab читает только существующие локальные данные.",
            ],
        }
        return report

    def analyze_trade(
        self,
        trade: Mapping[str, Any],
        index: int,
        previous_replays: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Replay one closed trade."""
        context = self.loader.context_for_trade(trade)
        decision = context.decision
        direction = str(trade.get("direction", "")).upper()
        symbol = symbol_full(str(trade.get("symbol", "")))
        scenario_data = self.scenarios.analyze(trade)
        engines = self.engine_context(direction, decision, context.diagnostics)
        trend = self.trend_context(direction, decision)
        news = self.classify_news(direction, context.news)
        actual_r = baseline_r(trade)
        improvements, worsened, best = self.evaluate_scenarios(
            result=trade_result(trade),
            baseline=actual_r,
            scenarios=scenario_data,
            engines=engines,
            trend=trend,
            news=news,
        )
        replay: dict[str, Any] = {
            "trade_id": self.trade_id(trade, index),
            "symbol": symbol,
            "direction": direction,
            "result": trade_result(trade),
            "pnl": safe_float(trade.get("pnl")),
            "opened_at": str(trade.get("opened_at", "")),
            "closed_at": str(trade.get("closed_at", "")),
            "entry": safe_float(trade.get("entry")),
            "exit": safe_float(trade.get("exit_price") or trade.get("exit")),
            "sl": safe_float(trade.get("stop_loss") or trade.get("sl")),
            "tp": safe_float(trade.get("take_profit") or trade.get("tp")),
            "baseline_r": actual_r,
            "decision_available": bool(decision),
            "decision_timestamp": self.iso_time(decision.get("_time")),
            "signal": str(decision.get("signal", "")),
            "score": safe_float(decision.get("score")),
            "confidence": safe_float(decision.get("confidence")),
            "quality": str(decision.get("quality", "")),
            "weighted_score": safe_float(decision.get("weighted_score")),
            "edge": safe_float(decision.get("edge")),
            "atr": safe_float(scenario_data.get("atr")),
            "engines": engines,
            "trend": trend,
            "momentum": {
                "status": engines.get("momentum", "UNKNOWN"),
                "phase": scenario_data.get("path", {}).get("momentum_phase", "DATA_UNAVAILABLE"),
                "phase_label": scenario_data.get("path", {}).get("momentum_phase_label", "Недостаточно данных"),
            },
            "news": news,
            "market_regime": trend.get("market_regime", "Недостаточно данных"),
            "heatmap": context.heatmap,
            "scenarios": scenario_data,
            "ohlcv_available": bool(scenario_data.get("ohlcv_available")),
            "improvements": improvements,
            "what_would_worsen": worsened,
            "improvement_score": sum(int(item.get("score", 0)) for item in improvements),
            "best_variant": best.get("variant", "NO_CONFIRMED_VARIANT"),
            "best_variant_delta_r": best.get("delta_r", 0.0),
            "atr_variant": self.best_family_variant(scenario_data.get("atr_stop_variants", [])),
            "tp_variant": self.best_family_variant(scenario_data.get("take_profit_variants", [])),
            "existing_loss_analysis": context.prior_loss_case,
            "explanation": self.explanation_context(context.explanation),
        }
        replay["memory"] = ReplayMemory.compare(replay, previous_replays)
        replay["memory"]["existing_report_available"] = bool(self.loader.memory_report)
        replay["verdict"] = build_verdict(replay)
        replay["main_reason"] = replay["verdict"].get("main_reason", "")
        replay["main_reason_label"] = replay["verdict"].get(
            "main_reason_label", ""
        )
        replay["trend_alignment"] = trend.get("alignment", "DATA_UNAVAILABLE")
        replay["news_status"] = news.get("status", "NEWS_NEUTRAL")
        replay["replay_confidence"] = replay_confidence(
            decision=decision,
            diagnostics=context.diagnostics,
            ohlcv_available=bool(scenario_data.get("ohlcv_available")),
            exit_available=safe_float(replay.get("exit")) > 0,
            news_available=bool(news.get("available")),
            trend_timeframes=sum(
                1 for value in trend.get("timeframes", {}).values()
                if value in {"LONG", "SHORT"}
            ),
        )
        return replay

    @staticmethod
    def engine_context(
        direction: str,
        decision: Mapping[str, Any],
        diagnostics: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return PASS/FAIL from diagnostics with score-based fallback."""
        result: dict[str, Any] = {}
        for engine in ("trend", "structure", "momentum", "risk"):
            status = str(diagnostics.get(engine, "")).upper()
            if status not in {"PASS", "FAIL"}:
                selected = safe_float(decision.get(f"{engine}_{direction.lower()}"))
                opposite_name = "short" if direction == "LONG" else "long"
                opposite = safe_float(decision.get(f"{engine}_{opposite_name}"))
                status = "PASS" if selected > 0 and selected >= opposite else "FAIL"
                if not decision:
                    status = "UNKNOWN"
            result[engine] = status
        result["primary_blocker"] = str(diagnostics.get("primary_blocker", ""))
        result["lost_score"] = safe_float(diagnostics.get("lost_score"))
        result["potential_score"] = safe_float(diagnostics.get("potential_score"))
        return result

    @staticmethod
    def trend_context(direction: str, decision: Mapping[str, Any]) -> dict[str, Any]:
        """Reconstruct 1H/4H/1D direction from existing Trend explanation."""
        scores: dict[str, dict[str, float]] = {
            "1h": {"LONG": 0.0, "SHORT": 0.0},
            "4h": {"LONG": 0.0, "SHORT": 0.0},
            "1d": {"LONG": 0.0, "SHORT": 0.0},
        }
        reason = str(decision.get("trend_reason", ""))
        for timeframe, side, points in TREND_PATTERN.findall(reason):
            scores[timeframe.lower()][side.upper()] += safe_float(points)
        timeframes: dict[str, str] = {}
        for timeframe, values in scores.items():
            if values["LONG"] > values["SHORT"]:
                timeframes[timeframe] = "LONG"
            elif values["SHORT"] > values["LONG"]:
                timeframes[timeframe] = "SHORT"
            else:
                timeframes[timeframe] = "UNKNOWN"
        available = [value for value in timeframes.values() if value in {"LONG", "SHORT"}]
        opposite = "SHORT" if direction == "LONG" else "LONG"
        if available and all(value == direction for value in available):
            alignment = "WITH_TREND"
        elif available and sum(value == opposite for value in available) > len(available) / 2:
            alignment = "AGAINST_TREND"
        elif available:
            alignment = "MIXED"
        else:
            alignment = "DATA_UNAVAILABLE"
        if available and all(value == "LONG" for value in available):
            regime = "BULLISH"
        elif available and all(value == "SHORT" for value in available):
            regime = "BEARISH"
        elif available:
            regime = "MIXED"
        else:
            regime = "Недостаточно данных"
        return {
            "timeframes": timeframes,
            "alignment": alignment,
            "market_regime": regime,
            "source": "decision_debug.trend_reason" if reason else "DATA_UNAVAILABLE",
        }

    @staticmethod
    def classify_news(direction: str, news: Mapping[str, Any]) -> dict[str, Any]:
        """Classify nearby news relative to trade direction."""
        result = dict(news)
        if not news.get("available"):
            result["status"] = "NEWS_NEUTRAL"
            return result
        sentiment = str(news.get("sentiment", "Neutral")).upper()
        strength = safe_float(news.get("strength"))
        supportive = (direction == "LONG" and sentiment == "BULLISH") or (
            direction == "SHORT" and sentiment == "BEARISH"
        )
        conflict = (direction == "LONG" and sentiment == "BEARISH") or (
            direction == "SHORT" and sentiment == "BULLISH"
        )
        if strength >= 4:
            status = "NEWS_RISK"
        elif supportive:
            status = "NEWS_SUPPORTIVE"
        elif conflict:
            status = "NEWS_CONFLICT"
        else:
            status = "NEWS_NEUTRAL"
        result["status"] = status
        return result

    @staticmethod
    def evaluate_scenarios(
        result: str,
        baseline: float,
        scenarios: Mapping[str, Any],
        engines: Mapping[str, Any],
        trend: Mapping[str, Any],
        news: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        """Compare each isolated family with baseline R."""
        improvements: list[dict[str, Any]] = []
        worsened: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        families = (
            ("ATR_STOP", "ATR Stop", scenarios.get("atr_stop_variants", [])),
            ("TAKE_PROFIT", "Take Profit", scenarios.get("take_profit_variants", [])),
        )
        for family, label, variants in families:
            for variant in variants:
                if not variant.get("available"):
                    continue
                delta = round(safe_float(variant.get("outcome_r")) - baseline, 4)
                row = {
                    "family": family,
                    "label": str(variant.get("variant") or label),
                    "variant": str(variant.get("variant") or label),
                    "outcome": variant.get("outcome", ""),
                    "outcome_r": safe_float(variant.get("outcome_r")),
                    "delta_r": delta,
                }
                candidates.append(row)
                if delta <= -0.5:
                    worsened.append(row)
            family_rows = [row for row in candidates if row.get("family") == family]
            if family_rows:
                best = max(family_rows, key=lambda row: safe_float(row.get("delta_r")))
                if safe_float(best.get("delta_r")) >= 0.5:
                    improvements.append({**best, "score": 1})

        for family, label, scenario in (
            ("NO_TP", "Без Take Profit", scenarios.get("no_take_profit", {})),
            ("TRAILING", "Trailing", scenarios.get("trailing_stop", {})),
        ):
            if not scenario.get("available"):
                continue
            delta = round(safe_float(scenario.get("outcome_r")) - baseline, 4)
            row = {
                "family": family,
                "label": label,
                "variant": label,
                "outcome": scenario.get("outcome", ""),
                "outcome_r": safe_float(scenario.get("outcome_r")),
                "delta_r": delta,
            }
            candidates.append(row)
            if delta >= 0.5:
                improvements.append({**row, "score": 1})
            elif delta <= -0.5:
                worsened.append(row)

        if result == "LOSS" and engines.get("momentum") == "FAIL":
            improvements.append({
                "family": "MOMENTUM_FILTER",
                "label": "Momentum Filter",
                "variant": "Пропуск Momentum FAIL",
                "outcome": "SKIPPED_LOSS",
                "outcome_r": 0.0,
                "delta_r": round(-baseline, 4),
                "score": 1,
            })
        if result == "LOSS" and trend.get("alignment") == "AGAINST_TREND":
            improvements.append({
                "family": "TREND_ALIGNMENT",
                "label": "Trend Alignment",
                "variant": "Пропуск входа против Trend",
                "outcome": "SKIPPED_LOSS",
                "outcome_r": 0.0,
                "delta_r": round(-baseline, 4),
                "score": 1,
            })
        if result == "LOSS" and news.get("status") == "NEWS_CONFLICT":
            improvements.append({
                "family": "NEWS_VETO",
                "label": "News Veto",
                "variant": "Пропуск NEWS_CONFLICT",
                "outcome": "SKIPPED_LOSS",
                "outcome_r": 0.0,
                "delta_r": round(-baseline, 4),
                "score": 1,
            })
        unique_improvements = list({
            (item["family"], item["variant"]): item for item in improvements
        }.values())
        all_best = [*candidates, *unique_improvements]
        best = max(all_best, key=lambda row: safe_float(row.get("delta_r")), default={})
        worsened.sort(key=lambda row: safe_float(row.get("delta_r")))
        return unique_improvements, worsened[:10], best

    @staticmethod
    def best_family_variant(rows: Any) -> str:
        """Return best available variant label by outcome R."""
        if not isinstance(rows, list):
            return "DATA_UNAVAILABLE"
        available = [row for row in rows if row.get("available")]
        if not available:
            return "DATA_UNAVAILABLE"
        best = max(available, key=lambda row: safe_float(row.get("outcome_r")))
        return str(best.get("variant", "DATA_UNAVAILABLE"))

    @staticmethod
    def explanation_context(explanation: Mapping[str, Any]) -> dict[str, Any]:
        """Keep only serializable explanation fields."""
        return {
            "passed": str(explanation.get("passed", "")),
            "failed": str(explanation.get("failed", "")),
            "reasons": str(explanation.get("reasons", "")),
            "summary": str(explanation.get("summary", "")),
        }

    @staticmethod
    def trade_id(trade: Mapping[str, Any], index: int) -> str:
        symbol = symbol_full(str(trade.get("symbol", ""))).replace("/USDT", "")
        opened = str(trade.get("opened_at", "")).replace(":", "").replace("-", "")[:15]
        return f"{index + 1:04d}_{symbol}_{opened}"

    @staticmethod
    def iso_time(value: Any) -> str:
        parsed = value if isinstance(value, datetime) else parse_time(value)
        return parsed.isoformat() if parsed else ""

    @staticmethod
    def status(replays: list[Mapping[str, Any]], ohlcv_count: int) -> str:
        if not replays:
            return "NO_DATA"
        if ohlcv_count < len(replays):
            return "PARTIAL"
        return "OK"
