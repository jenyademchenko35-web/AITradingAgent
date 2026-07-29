"""Read-only post-trade intelligence and probable-cause analytics."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


CAUSES = (
    "LATE_ENTRY", "WEAK_VOLUME", "SIDEWAYS_MARKET", "HIGH_VOLATILITY",
    "WEAK_MOMENTUM", "TREND_MISMATCH", "STRUCTURE_FAILURE", "RISK_TOO_HIGH",
    "STOP_TOO_TIGHT", "TAKE_PROFIT_TOO_FAR", "NEWS_OR_SPIKE",
    "GOOD_SETUP_BAD_OUTCOME", "UNKNOWN",
)
LIVE_FILES = (
    "multi_timeframe_agent_v3.py", "portfolio_manager.py", "telegram_bot_v4.py",
    "watchdog_agent.py", "strategy_weights.json", "portfolio_config.json",
)


class TradeIntelligenceAgent:
    """Analyze existing artifacts without mutating trading state."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root)
        self.reports = self.root / "reports"
        self.warnings: list[str] = []
        self.source_status: dict[str, Any] = {}

    def analyze(self, *, write_reports: bool = True) -> dict[str, Any]:
        before = self._live_hashes()
        trades = self._load_trades()
        context_rows = self._load_context_rows()
        ohlcv = self._load_ohlcv()
        analyzed = [
            self._analyze_trade(trade, context_rows, ohlcv)
            for trade in trades
        ]
        # Stable ID deduplication makes repeated runs idempotent.
        analyzed = list({row["trade_id"]: row for row in analyzed}.values())
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "READ_ONLY_ANALYTICS",
            "source_status": self.source_status,
            "warnings": sorted(set(self.warnings)),
            "trades": analyzed,
        }
        summary = self.build_summary(analyzed)
        if write_reports:
            self._write_reports(payload, summary)
        if before != self._live_hashes():
            raise RuntimeError("read-only safety violation: a live file changed")
        return {"analysis": payload, "summary": summary}

    def trade(self, trade_id: str) -> dict[str, Any] | None:
        result = self.analyze(write_reports=False)
        return next(
            (row for row in result["analysis"]["trades"]
             if row["trade_id"] == trade_id),
            None,
        )

    def build_summary(self, trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        values = [self._number(row.get("pnl_r")) for row in trades]
        wins = sum(value > 0 for value in values)
        losses = sum(value < 0 for value in values)
        base = self._performance(values)
        loss_rows = [row for row in trades if self._number(row.get("pnl_r")) < 0]
        win_rows = [row for row in trades if self._number(row.get("pnl_r")) > 0]
        summary = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "READ_ONLY_ANALYTICS",
            "total_analyzed": len(trades),
            "wins": wins,
            "losses": losses,
            "winrate": wins / len(trades) if trades else 0.0,
            "profit_factor": base["profit_factor"],
            "net_r": base["net_r"],
            "context_matching": {
                "full": sum(row.get("context_match_status") == "FULL" for row in trades),
                "partial": sum(row.get("context_match_status") == "PARTIAL" for row in trades),
                "missing": sum(row.get("context_match_status") == "MISSING" for row in trades),
            },
            "loss_causes": self._cause_counts(loss_rows),
            "win_causes": self._cause_counts(win_rows),
            "causes_by_symbol": self._group_causes(trades, lambda row: row.get("symbol")),
            "causes_by_market_regime": self._group_causes(
                trades, lambda row: row.get("context", {}).get("market_regime", "UNKNOWN")
            ),
            "causes_by_holding": self._group_causes(
                trades, lambda row: self._holding_bucket(row.get("holding_candles"))
            ),
            "causes_by_side": self._group_causes(trades, lambda row: row.get("side")),
            "causes_by_score_bucket": self._group_causes(
                trades, lambda row: self._score_bucket(row.get("context", {}).get("total_score"))
            ),
            "causes_by_confidence_bucket": self._group_causes(
                trades, lambda row: self._confidence_bucket(
                    row.get("context", {}).get("confidence")
                )
            ),
            "average_mfe_r": self._average(
                row.get("behavior", {}).get("mfe_r") for row in trades
            ),
            "average_mae_r": self._average(
                row.get("behavior", {}).get("mae_r") for row in trades
            ),
            "near_tp_share": self._share(
                trades, lambda row: row.get("behavior", {}).get("near_tp") is True
            ),
            "late_entry_share": self._share(
                trades, lambda row: row.get("behavior", {}).get("late_entry") is True
            ),
            "weak_volume_share": self._share(
                trades, lambda row: row.get("behavior", {}).get("weak_volume") is True
            ),
            "sideways_share": self._share(
                trades, lambda row: row.get("behavior", {}).get("sideways") is True
            ),
            "what_if_filters": self.what_if(trades),
        }
        summary["recommendations"] = self._recommendations(trades, summary)
        return summary

    def classify(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Return probable causes; inputs may be synthetic for deterministic tests."""
        context = row.get("context", row)
        behavior = row.get("behavior", row)
        result = str(row.get("result", "")).upper()
        score = self._number(context.get("total_score", context.get("score")))
        confidence = self._number(context.get("confidence"))
        volume = self._optional_number(context.get("volume_ratio"))
        adx = self._optional_number(context.get("adx"))
        momentum = self._optional_number(context.get("momentum"))
        risk_score = self._optional_number(context.get("risk_score"))
        regime = str(context.get("market_regime", "")).upper()
        trend = str(context.get("trend", "")).upper()
        side = str(row.get("side", row.get("direction", ""))).upper()
        structure = str(context.get("structure", "")).upper()
        causes: list[tuple[str, float, str]] = []

        def add(cause: str, confidence_value: float, evidence: str) -> None:
            causes.append((cause, confidence_value, evidence))

        if behavior.get("late_entry") is True:
            add("LATE_ENTRY", 0.76, "entry followed an extended move with limited MFE")
        if volume is not None and volume < 1.0:
            add("WEAK_VOLUME", 0.72, f"volume ratio {volume:.2f} below 1.0")
        if behavior.get("sideways") is True or regime in {"SIDEWAYS", "FLAT", "RANGE"}:
            add("SIDEWAYS_MARKET", 0.74, "market regime was sideways/range")
        if behavior.get("high_volatility") is True:
            add("HIGH_VOLATILITY", 0.70, "ATR volatility was elevated")
        if momentum is not None and momentum < 0:
            add("WEAK_MOMENTUM", 0.66, "momentum score was weak")
        if (
            (side == "LONG" and trend in {"BEARISH", "DOWN"})
            or (side == "SHORT" and trend in {"BULLISH", "UP"})
        ):
            add("TREND_MISMATCH", 0.78, f"{side} entry opposed {trend} trend")
        if structure in {"FAILED", "WEAK", "BROKEN"}:
            add("STRUCTURE_FAILURE", 0.69, "structure was weak or failed")
        if risk_score is not None and risk_score < 0:
            add("RISK_TOO_HIGH", 0.68, "risk score was negative")
        if behavior.get("stop_too_tight") is True:
            add("STOP_TOO_TIGHT", 0.73, "MAE exceeded stop distance before recovery")
        if behavior.get("take_profit_too_far") is True:
            add("TAKE_PROFIT_TOO_FAR", 0.67, "favorable excursion stayed well below TP")
        if behavior.get("news_or_spike") is True:
            add("NEWS_OR_SPIKE", 0.65, "abnormal candle or volatility spike detected")
        good = (
            result == "LOSS" and score >= 25 and confidence >= 80
            and (volume is None or volume >= 1)
            and trend not in {"MISMATCH", "BEARISH" if side == "LONG" else "BULLISH"}
        )
        if good and not causes:
            add(
                "GOOD_SETUP_BAD_OUTCOME", 0.60,
                "setup scores were strong but the realized outcome was adverse",
            )
        if not causes:
            add("UNKNOWN", 0.25, "available evidence is insufficient for attribution")
        causes.sort(key=lambda item: item[1], reverse=True)
        return {
            "primary_cause": causes[0][0],
            "secondary_causes": [cause for cause, _, _ in causes[1:]],
            "cause_confidence": causes[0][1],
            "evidence": [evidence for _, _, evidence in causes],
            "warnings": [
                "Probable cause only; rule-based attribution is not causal proof."
            ],
        }

    def what_if(self, trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        filters: dict[str, Callable[[Mapping[str, Any]], bool]] = {
            "ADX_LT_18": lambda row: self._number(row.get("context", {}).get("adx"), 999) < 18,
            "VOLUME_RATIO_LT_1": lambda row: self._number(
                row.get("context", {}).get("volume_ratio"), 999
            ) < 1,
            "SIDEWAYS_REGIME": lambda row: row.get("behavior", {}).get("sideways") is True,
            "CONFIDENCE_LT_60": lambda row: self._number(
                row.get("context", {}).get("confidence"), 999
            ) < 60,
            "SCORE_BELOW_CURRENT_THRESHOLD": lambda row: self._number(
                row.get("context", {}).get("total_score"), 999
            ) < 25,
            "HIGH_VOLATILITY": lambda row: row.get("behavior", {}).get(
                "high_volatility"
            ) is True,
            "LATE_ENTRY": lambda row: row.get("behavior", {}).get("late_entry") is True,
        }
        before = self._performance([self._number(row.get("pnl_r")) for row in trades])
        output = {}
        for name, predicate in filters.items():
            kept = [row for row in trades if not predicate(row)]
            after = self._performance([self._number(row.get("pnl_r")) for row in kept])
            output[name] = {
                "removed_trades": len(trades) - len(kept),
                "remaining_trades": len(kept),
                "winrate_before": before["winrate"],
                "winrate_after": after["winrate"],
                "profit_factor_before": before["profit_factor"],
                "profit_factor_after": after["profit_factor"],
                "net_r_before": before["net_r"],
                "net_r_after": after["net_r"],
                "max_drawdown_before": before["max_drawdown_r"],
                "max_drawdown_after": after["max_drawdown_r"],
            }
        return output

    def _analyze_trade(
        self,
        trade: Mapping[str, Any],
        context_rows: Sequence[Mapping[str, Any]],
        ohlcv: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> dict[str, Any]:
        item = self._normalize_trade(trade)
        context, context_status = self._context_for(item, context_rows)
        behavior, market_complete = self._behavior(item, ohlcv)
        if context_status == "FULL" and not market_complete:
            context_status = "PARTIAL"
        elif context_status == "MISSING" and market_complete:
            context_status = "PARTIAL"
        row = {**item, "context": context, "behavior": behavior}
        row.update(self.classify(row))
        row["context_match_status"] = context_status
        row["probable_cause_label"] = f"probable cause: {row['primary_cause']}"
        return row

    def _normalize_trade(self, row: Mapping[str, Any]) -> dict[str, Any]:
        entry = self._number(self._first(row, "entry_price", "entry"))
        exit_price = self._number(self._first(row, "exit_price", "exit"))
        opened = str(self._first(row, "opened_at", "entry_time", "timestamp") or "")
        closed = str(self._first(row, "closed_at", "exit_time") or "")
        trade_id = str(row.get("trade_id") or self._trade_id(row))
        side = str(self._first(row, "direction", "side") or "UNKNOWN").upper()
        pnl_r = self._number(self._first(row, "pnl_r", "R", "r"))
        result = str(row.get("result") or ("WIN" if pnl_r > 0 else "LOSS" if pnl_r < 0 else "FLAT")).upper()
        holding_seconds = self._seconds(opened, closed)
        return {
            "trade_id": trade_id, "symbol": str(row.get("symbol", "UNKNOWN")),
            "side": side, "entry_time": opened, "exit_time": closed,
            "entry_price": entry, "exit_price": exit_price, "result": result,
            "pnl": self._number(row.get("pnl")), "pnl_r": pnl_r,
            "holding_time_seconds": holding_seconds,
            "holding_candles": self._number(
                row.get("holding_candles"),
                round(holding_seconds / 3600) if holding_seconds else 0,
            ),
            "stop_loss": self._number(self._first(row, "stop_loss", "sl")),
            "take_profit": self._number(self._first(row, "take_profit", "tp")),
            "_source": dict(row),
        }

    def _context_for(
        self, trade: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[str, Any], str]:
        symbol = self._canonical_symbol(trade.get("symbol"))
        entry = self._timestamp(trade.get("entry_time"))
        candidates = []
        for row in rows:
            if self._canonical_symbol(row.get("symbol")) != symbol:
                continue
            timestamp = self._timestamp(row.get("timestamp"))
            if timestamp is not None and entry is not None and timestamp <= entry:
                candidates.append((timestamp, row))
        source = dict(max(candidates, key=lambda item: item[0])[1]) if candidates else dict(
            trade.get("_source", {})
        )
        context = {
            "atr": self._optional_number(self._first(source, "atr")),
            "adx": self._optional_number(self._first(source, "adx")),
            "rsi": self._optional_number(self._first(source, "rsi")),
            "volume_ratio": self._optional_number(
                self._first(source, "volume_ratio", "relative_volume")
            ),
            "ema50": self._optional_number(self._first(source, "ema50")),
            "ema200": self._optional_number(self._first(source, "ema200")),
            "trend": self._first(source, "trend", "trend_alignment"),
            "structure": self._first(source, "structure", "structure_reason"),
            "momentum": self._optional_number(
                self._first(source, "momentum_score", "momentum")
            ),
            "risk_score": self._optional_number(
                self._first(source, "risk_score", "risk")
            ),
            "total_score": self._optional_number(
                self._first(source, "total_score", "score")
            ),
            "confidence": self._optional_number(source.get("confidence")),
            "market_regime": self._first(source, "market_regime") or "UNKNOWN",
            "volatility_regime": self._first(
                source, "volatility_regime", "volatility"
            ) or "UNKNOWN",
            "signal_quality": self._first(source, "quality", "signal_quality"),
            "primary_blocker": self._first(source, "primary_blocker"),
            "secondary_blockers": self._split(
                self._first(source, "secondary_blockers", "failed")
            ),
        }
        known = sum(value not in (None, "", "UNKNOWN", []) for value in context.values())
        return context, "FULL" if known >= 8 else "PARTIAL" if known else "MISSING"

    def _behavior(
        self,
        trade: Mapping[str, Any],
        caches: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> tuple[dict[str, Any], bool]:
        rows = caches.get(self._canonical_symbol(trade.get("symbol")), [])
        start, end = self._timestamp(trade.get("entry_time")), self._timestamp(
            trade.get("exit_time")
        )
        selected = [
            row for row in rows
            if start is not None and end is not None
            and (timestamp := self._timestamp(row.get("timestamp"))) is not None
            and start <= timestamp <= end
        ]
        complete = bool(
            selected
            and self._timestamp(selected[-1].get("timestamp")) + timedelta(hours=1) >= end
        )
        entry = self._number(trade.get("entry_price"))
        side = str(trade.get("side")).upper()
        risk = abs(entry - self._number(trade.get("stop_loss")))
        if risk <= 0:
            risk = 0.0
        if selected:
            if side == "LONG":
                mfe = max(self._number(row.get("high")) - entry for row in selected)
                mae = max(entry - self._number(row.get("low")) for row in selected)
            else:
                mfe = max(entry - self._number(row.get("low")) for row in selected)
                mae = max(self._number(row.get("high")) - entry for row in selected)
        else:
            mfe = mae = None
        tp = self._number(trade.get("take_profit"))
        tp_distance = abs(tp - entry)
        mfe_r = mfe / risk if mfe is not None and risk else None
        mae_r = mae / risk if mae is not None and risk else None
        distance_to_tp = max(0.0, tp_distance - mfe) if mfe is not None else None
        source = trade.get("_source", {})
        volume = self._optional_number(source.get("volume_ratio"))
        volatility = self._optional_number(
            self._first(source, "volatility", "atr_pct")
        )
        regime = str(source.get("market_regime", "")).upper()
        late = bool(
            source.get("late_entry") is True
            or (mfe_r is not None and mfe_r < 0.3 and mae_r is not None and mae_r > 0.8)
        )
        return {
            "maximum_favorable_excursion": mfe,
            "maximum_adverse_excursion": mae,
            "mfe_r": mfe_r, "mae_r": mae_r,
            "distance_to_tp_at_best_moment": distance_to_tp,
            "near_tp": (
                mfe is not None and tp_distance > 0 and mfe >= tp_distance * 0.85
            ),
            "early_reversal": (
                mae_r is not None and mae_r >= 0.8 and (mfe_r or 0) < 0.3
            ),
            "late_entry": late,
            "high_volatility": bool(
                source.get("high_volatility") is True
                or (volatility is not None and volatility >= 2.0)
            ),
            "sideways": regime in {"SIDEWAYS", "FLAT", "RANGE"},
            "weak_volume": volume is not None and volume < 1.0,
            "stop_too_tight": mae_r is not None and mae_r > 1 and (mfe_r or 0) > 0.5,
            "take_profit_too_far": (
                mfe is not None and tp_distance > 0 and mfe < tp_distance * 0.4
            ),
            "news_or_spike": bool(source.get("news_or_spike") is True),
            "market_path_complete": complete,
            "market_candles": len(selected),
        }, complete

    def _load_trades(self) -> list[dict[str, Any]]:
        enriched = self._json(self.reports / "research_trades_enriched.json")
        rows = enriched.get("trades", []) if isinstance(enriched, Mapping) else []
        if rows:
            self.source_status["closed_trades"] = {
                "path": "reports/research_trades_enriched.json", "rows": len(rows)
            }
            return [dict(row) for row in rows if isinstance(row, Mapping)]
        rows = self._csv(self.root / "trades.csv")
        closed = [
            row for row in rows
            if str(row.get("status", "")).upper() not in {"OPEN", "ACTIVE"}
            and row.get("closed_at")
        ]
        self.source_status["closed_trades"] = {
            "path": "trades.csv", "rows": len(closed)
        }
        return closed

    def _load_context_rows(self) -> list[dict[str, Any]]:
        rows = []
        for filename in ("signals_v3.csv", "decision_debug.csv", "decision_explanations.csv"):
            loaded = self._csv(self.root / filename)
            self.source_status[filename] = {"rows": len(loaded)}
            rows.extend(loaded)
        for filename in (
            "active_setups_v3.json", "agent_v3_stats.json",
            "diagnostics_report.json", "calibration_report.json",
        ):
            payload = self._json(self.root / filename)
            self.source_status[filename] = {"available": bool(payload)}
        return rows

    def _load_ohlcv(self) -> dict[str, list[dict[str, Any]]]:
        output = {}
        cache_dir = self.root / "ohlcv_cache"
        for path in sorted(cache_dir.glob("*_1h.csv")) if cache_dir.exists() else []:
            symbol = path.stem.split("_1h")[0].replace("_", "")
            output[symbol] = self._csv(path)
        self.source_status["ohlcv_cache"] = {
            "symbols": sorted(output), "count": len(output)
        }
        return output

    def _write_reports(
        self, analysis: Mapping[str, Any], summary: Mapping[str, Any]
    ) -> None:
        self.reports.mkdir(parents=True, exist_ok=True)
        (self.reports / "trade_intelligence.json").write_text(
            json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (self.reports / "trade_intelligence_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        matching = summary["context_matching"]
        lines = [
            "TRADE INTELLIGENCE — READ-ONLY ANALYTICS",
            f"Analyzed: {summary['total_analyzed']}",
            f"Wins / Losses: {summary['wins']} / {summary['losses']}",
            f"WinRate: {summary['winrate']:.2%}",
            f"Profit Factor: {summary['profit_factor']}",
            f"Net R: {summary['net_r']:.3f}",
            f"Context FULL/PARTIAL/MISSING: {matching['full']}/{matching['partial']}/{matching['missing']}",
            f"Loss probable causes: {summary['loss_causes']}",
            "",
            "Recommendations are analytical only and are never applied automatically.",
        ]
        (self.reports / "trade_intelligence_summary.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def _recommendations(
        self, trades: Sequence[Mapping[str, Any]], summary: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        recommendations = []
        for cause, count in summary["loss_causes"].items():
            if cause in {"UNKNOWN", "GOOD_SETUP_BAD_OUTCOME"} or count == 0:
                continue
            sample = len(trades)
            strong = sample >= 30 and count >= max(5, sample * 0.15)
            recommendations.append({
                "recommendation": f"Investigate a research-only veto for {cause}",
                "evidence": f"{count} losing trades have probable cause {cause}",
                "sample_size": sample,
                "confidence": "MEDIUM" if strong else "LOW",
                "expected_effect": "Requires out-of-sample what-if validation",
                "warning": (
                    "Do not change live logic automatically."
                    if strong else
                    "Insufficient sample for a strong recommendation; do not change live logic."
                ),
            })
        return recommendations

    @staticmethod
    def _performance(values: Sequence[float]) -> dict[str, Any]:
        wins = [value for value in values if value > 0]
        losses = [value for value in values if value < 0]
        equity = peak = drawdown = 0.0
        for value in values:
            equity += value
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        return {
            "winrate": len(wins) / len(values) if values else 0.0,
            "profit_factor": (
                sum(wins) / abs(sum(losses)) if losses
                else "INF" if wins else None
            ),
            "net_r": sum(values),
            "max_drawdown_r": drawdown,
        }

    @staticmethod
    def _cause_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        counts = Counter(str(row.get("primary_cause", "UNKNOWN")) for row in rows)
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def _group_causes(
        self,
        rows: Sequence[Mapping[str, Any]],
        key: Callable[[Mapping[str, Any]], Any],
    ) -> dict[str, dict[str, int]]:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(key(row) or "UNKNOWN")].append(row)
        return {
            name: self._cause_counts(items) for name, items in sorted(grouped.items())
        }

    def _live_hashes(self) -> dict[str, str | None]:
        result = {}
        for filename in LIVE_FILES:
            path = self.root / filename
            result[filename] = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            )
        return result

    def _csv(self, path: Path) -> list[dict[str, Any]]:
        try:
            with path.open(newline="", encoding="utf-8") as stream:
                return [dict(row) for row in csv.DictReader(stream)]
        except (OSError, csv.Error, UnicodeError):
            self.warnings.append(f"Missing, empty, or unreadable source: {path.name}")
            return []

    def _json(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, UnicodeError):
            self.warnings.append(f"Missing, empty, or unreadable source: {path.name}")
            return {}

    @staticmethod
    def _first(row: Mapping[str, Any], *names: str) -> Any:
        for name in names:
            value = row.get(name)
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _number(value: Any, default: float = 0.0) -> float:
        try:
            number = float(value)
            return number if math.isfinite(number) else default
        except (TypeError, ValueError):
            return default

    @classmethod
    def _optional_number(cls, value: Any) -> float | None:
        return None if value in (None, "") else cls._number(value)

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _seconds(cls, start: Any, end: Any) -> float:
        left, right = cls._timestamp(start), cls._timestamp(end)
        return max(0.0, (right - left).total_seconds()) if left and right else 0.0

    @staticmethod
    def _canonical_symbol(value: Any) -> str:
        return str(value or "").upper().replace("/", "").replace("_", "").replace("-", "")

    @staticmethod
    def _split(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        return [item.strip() for item in str(value or "").replace("|", ",").split(",") if item.strip()]

    @staticmethod
    def _trade_id(row: Mapping[str, Any]) -> str:
        raw = "|".join(str(row.get(key, "")) for key in ("symbol", "opened_at", "entry", "direction"))
        return "TI-" + hashlib.sha256(raw.encode()).hexdigest()[:16]

    @staticmethod
    def _holding_bucket(value: Any) -> str:
        number = TradeIntelligenceAgent._number(value)
        return "0-3" if number <= 3 else "4-12" if number <= 12 else "13-48" if number <= 48 else "49+"

    @staticmethod
    def _score_bucket(value: Any) -> str:
        number = TradeIntelligenceAgent._number(value)
        return "<20" if number < 20 else "20-24" if number < 25 else "25-27" if number < 28 else "28+"

    @staticmethod
    def _confidence_bucket(value: Any) -> str:
        number = TradeIntelligenceAgent._number(value)
        return "<60" if number < 60 else "60-79" if number < 80 else "80-89" if number < 90 else "90+"

    @classmethod
    def _average(cls, values: Iterable[Any]) -> float | None:
        numbers = [cls._number(value) for value in values if value is not None]
        return sum(numbers) / len(numbers) if numbers else None

    @staticmethod
    def _share(rows: Sequence[Any], predicate: Callable[[Any], bool]) -> float:
        return sum(predicate(row) for row in rows) / len(rows) if rows else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--analyze", action="store_true")
    group.add_argument("--summary", action="store_true")
    group.add_argument("--trade")
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    agent = TradeIntelligenceAgent(args.root)
    if args.trade:
        print(json.dumps(agent.trade(args.trade) or {"error": "trade not found"}, ensure_ascii=False, indent=2))
    else:
        result = agent.analyze(write_reports=args.analyze)
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
