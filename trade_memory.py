"""Trade Memory for AITradingAgent.

Finds historical closed trades similar to the latest setup for a symbol. This
is read-only and does not affect trading.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from market_intelligence_utils import (
    BASE_DIR,
    latest_by_symbol,
    nearest_before,
    normalize_decision,
    read_csv_rows,
    safe_float,
    symbol_full,
    trade_result,
    utc_now,
    write_csv,
    write_json,
)
from report_metadata import build_report_metadata, timestamp_bounds
from trade_metrics_normalizer import (
    aggregate_trade_metrics,
    canonical_symbol,
    canonical_timestamp,
    is_closed_trade,
    max_drawdown_r,
    normalize_trade,
    optional_float,
    trade_identity_keys,
)


TRADES_FILE = BASE_DIR / "trades.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
JSON_OUTPUT = BASE_DIR / "trade_memory_report.json"
SUMMARY_OUTPUT = BASE_DIR / "trade_memory_summary.txt"
MATCHES_CSV = BASE_DIR / "trade_memory_matches.csv"
NORMALIZED_METRICS_FILE = BASE_DIR / "normalized_trade_metrics.csv"

FIELDS = [
    "trade_id",
    "source_row_id",
    "source_index",
    "target_symbol",
    "matched_symbol",
    "direction",
    "result",
    "entry",
    "stop_loss",
    "take_profit",
    "exit_price",
    "pnl_percent",
    "pnl_r",
    "metrics_status",
    "incomplete_reasons",
    "raw_pnl",
    "opened_at",
    "closed_at",
    "metrics_linked",
    "match_quality",
    "score",
    "confidence",
    "quality",
    "edge",
    "momentum",
    "similarity",
    "reasons",
]


def calculate_subset_metrics(
    matches: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate only metrics proven to link to the similar-trade subset."""
    quality_counts = Counter(
        str(row.get("match_quality") or "MISSING") for row in matches
    )
    complete = []
    for row in matches:
        linked = row.get("metrics_linked") is True or str(
            row.get("metrics_linked", "")
        ).strip().upper() == "TRUE"
        pnl_r = optional_float(row.get("pnl_r"))
        if (
            linked
            and str(row.get("metrics_status", "")).upper() == "COMPLETE"
            and pnl_r is not None
        ):
            complete.append((row, pnl_r))

    complete.sort(
        key=lambda item: (
            canonical_timestamp(
                item[0].get("closed_at") or item[0].get("opened_at")
            ),
            int(optional_float(item[0].get("source_index")) or 0),
        )
    )
    pnl_r_values = [value for _, value in complete]
    all_wins = [row for row in matches if str(row.get("result", "")).upper() == "WIN"]
    all_losses = [
        row for row in matches if str(row.get("result", "")).upper() == "LOSS"
    ]
    wins_with_metrics = sum(
        str(row.get("result", "")).upper() == "WIN" for row, _ in complete
    )
    losses_with_metrics = sum(
        str(row.get("result", "")).upper() == "LOSS" for row, _ in complete
    )
    sum_positive_r = round(sum(value for value in pnl_r_values if value > 0), 6)
    sum_negative_r = round(sum(value for value in pnl_r_values if value < 0), 6)
    unmatched = quality_counts.get("MISSING", 0)
    unavailable_reason = ""
    profit_factor: float | None = None
    if not complete:
        unavailable_reason = "normalized metrics linkage missing"
    elif sum_negative_r < 0:
        profit_factor = round(sum_positive_r / abs(sum_negative_r), 6)
    else:
        unavailable_reason = "в полной выборке нет отрицательных R"

    warnings = []
    winrate = round(len(all_wins) / len(matches) * 100, 2) if matches else 0.0
    if winrate > 0 and complete and sum_positive_r == 0:
        warnings.append(
            "DATA_QUALITY_WARNING: WIN присутствуют, но sum_positive_r равен 0."
        )
    if unmatched:
        warnings.append(
            f"Не сопоставлены с normalized metrics: {unmatched}."
        )

    return {
        "trades": len(matches),
        "closed_trades": len(matches),
        "matched_similar_total": len(matches),
        "metrics_complete_total": len(complete),
        "complete_metric_sample_size": len(complete),
        "metrics_incomplete_total": len(matches) - len(complete),
        "incomplete_metrics": len(matches) - len(complete),
        "unmatched_similar_trades": unmatched,
        "wins": len(all_wins),
        "losses": len(all_losses),
        "wins_with_metrics": wins_with_metrics,
        "losses_with_metrics": losses_with_metrics,
        "winrate": winrate,
        "sum_positive_r": sum_positive_r,
        "sum_negative_r": sum_negative_r,
        "profit_factor": profit_factor,
        "profit_factor_unavailable_reason": unavailable_reason,
        "net_r": round(sum(pnl_r_values), 6) if complete else None,
        "average_r": (
            round(sum(pnl_r_values) / len(pnl_r_values), 6)
            if pnl_r_values
            else None
        ),
        "max_drawdown_r": max_drawdown_r(pnl_r_values) if complete else None,
        "subset_match_quality": dict(sorted(quality_counts.items())),
        "data_quality_warnings": warnings,
        "metric_unit": "R",
    }


class TradeMemory:
    """Search similar historical trade situations."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.trades_file = base_dir / TRADES_FILE.name
        self.debug_file = base_dir / DEBUG_FILE.name
        self.diagnostics_file = base_dir / DIAGNOSTICS_FILE.name
        self.normalized_metrics_file = base_dir / NORMALIZED_METRICS_FILE.name
        self.json_output = base_dir / JSON_OUTPUT.name
        self.summary_output = base_dir / SUMMARY_OUTPUT.name
        self.matches_csv = base_dir / MATCHES_CSV.name
        self.trade_rows = read_csv_rows(self.trades_file)
        self.normalized_rows = read_csv_rows(self.normalized_metrics_file)
        self.metrics_index = self._build_metrics_index(self.normalized_rows)
        self.debug_rows = [
            normalize_decision(row) for row in read_csv_rows(self.debug_file)
        ]
        self.latest = latest_by_symbol(read_csv_rows(self.debug_file))
        self.diagnostics = [
            {**row, "_time": normalize_decision(row).get("_time")}
            for row in read_csv_rows(self.diagnostics_file)
        ]

    def build_report(self, symbol: str | None = None) -> dict[str, Any]:
        """Build and save memory report."""
        target_symbol = symbol_full(symbol or self.best_symbol())
        target = normalize_decision(self.latest.get(target_symbol, {"symbol": target_symbol}))
        matches = self.find_matches(target)
        stats = calculate_subset_metrics(matches)
        portfolio_metrics = aggregate_trade_metrics(self.trade_rows)
        generated_at = utc_now()
        period_start, period_end = timestamp_bounds(
            matches,
            fields=("opened_at", "closed_at"),
        )
        report = {
            "generated_at": generated_at,
            "metadata": build_report_metadata(
                generator="trade_memory.TradeMemory",
                metric_unit="R",
                source_files=[
                    self.trades_file,
                    self.debug_file,
                    self.normalized_metrics_file,
                ],
                base_dir=self.base_dir,
                data_period_start=period_start,
                data_period_end=period_end,
                closed_trades_total=portfolio_metrics.get("closed_trades", 0),
                complete_metrics_total=portfolio_metrics.get("metrics_trades", 0),
                generated_at=generated_at,
            ),
            "status": "OK" if matches else "NO_MATCHES",
            "target": self.target_summary(target),
            "matches_count": len(matches),
            "metrics_scope": "similar_closed_trades",
            "subset_match_quality": stats.get("subset_match_quality", {}),
            "unmatched_similar_trades": stats.get("unmatched_similar_trades", 0),
            "complete_metric_sample_size": stats.get(
                "complete_metric_sample_size", 0
            ),
            "stats": stats,
            "portfolio_metrics": portfolio_metrics,
            "matches": matches[:50],
            "recommendation": self.recommendation(matches, stats),
            "restrictions": [
                "Trade Memory только сравнивает похожие ситуации.",
                "DecisionEngine и стратегия не менялись.",
            ],
        }
        write_json(self.json_output, report)
        write_csv(self.matches_csv, matches, FIELDS)
        self.summary_output.write_text(self.format_summary(report), encoding="utf-8")
        return report

    def best_symbol(self) -> str:
        """Pick latest symbol with highest confidence/score when no arg is given."""
        if not self.latest:
            return "BTC/USDT"
        ranked = sorted(
            (normalize_decision(row) for row in self.latest.values()),
            key=lambda row: (row.get("confidence", 0), row.get("weighted_score", 0), row.get("edge", 0)),
            reverse=True,
        )
        return str(ranked[0].get("symbol", "BTC/USDT"))

    def find_matches(self, target: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Find similar closed trades."""
        rows = []
        for index, trade in enumerate(self.trade_rows, start=1):
            if not is_closed_trade(trade):
                continue
            fallback = normalize_trade(trade, index)
            if fallback.get("result") not in {"WIN", "LOSS"}:
                continue
            decision = nearest_before(self.debug_rows, str(trade.get("symbol", "")), normalize_decision({"timestamp": trade.get("opened_at")}).get("_time"), max_hours=24)
            if not decision:
                continue
            similarity, reasons = self.similarity(target, decision)
            if similarity < 3:
                continue
            linked_metrics, match_quality = self._link_normalized_trade(trade, index)
            normalized = linked_metrics or fallback
            metrics_linked = linked_metrics is not None
            rows.append({
                "trade_id": normalized.get("trade_id", ""),
                "source_row_id": normalized.get("source_row_id", index),
                "source_index": normalized.get("source_index", index),
                "target_symbol": target.get("symbol", ""),
                "matched_symbol": trade.get("symbol", ""),
                "symbol": trade.get("symbol", ""),
                "direction": trade.get("direction", ""),
                "status": fallback.get("result") or trade_result(trade),
                "result": fallback.get("result") or trade_result(trade),
                "entry": normalized.get("entry", ""),
                "stop_loss": normalized.get("stop_loss", ""),
                "take_profit": normalized.get("take_profit", ""),
                "exit_price": normalized.get("exit_price", ""),
                "pnl_percent": normalized.get("pnl_percent", "") if metrics_linked else "",
                "pnl_r": normalized.get("pnl_r", "") if metrics_linked else "",
                "metrics_status": (
                    normalized.get("metrics_status", "INCOMPLETE")
                    if metrics_linked
                    else "UNMATCHED"
                ),
                "incomplete_reasons": (
                    normalized.get("incomplete_reasons", "")
                    if metrics_linked
                    else "normalized metrics linkage missing"
                ),
                "raw_pnl": normalized.get("raw_pnl", ""),
                "opened_at": trade.get("opened_at", ""),
                "closed_at": trade.get("closed_at", ""),
                "metrics_linked": metrics_linked,
                "match_quality": match_quality,
                "score": decision.get("score", ""),
                "confidence": decision.get("confidence", ""),
                "quality": decision.get("quality", ""),
                "edge": decision.get("edge", ""),
                "momentum": self.engine_status(decision, "momentum"),
                "similarity": similarity,
                "reasons": " | ".join(reasons),
            })
        return sorted(rows, key=lambda row: safe_float(row.get("similarity")), reverse=True)

    @staticmethod
    def _build_metrics_index(
        rows: list[Mapping[str, Any]],
    ) -> dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]]:
        """Index normalized rows by every supported stable trade identity."""
        index: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}
        for row in rows:
            normalized_row = dict(row)
            for quality, key in trade_identity_keys(normalized_row):
                index.setdefault((quality, key), []).append(normalized_row)
        return index

    def _link_normalized_trade(
        self,
        trade: Mapping[str, Any],
        source_index: int,
    ) -> tuple[dict[str, Any] | None, str]:
        """Link a source trade to one normalized metric row conservatively."""
        for quality, key in trade_identity_keys(trade, source_index):
            candidates = self.metrics_index.get((quality, key), [])
            compatible = [
                row for row in candidates if self._identities_compatible(trade, row)
            ]
            if len(compatible) == 1:
                return compatible[0], quality
        return None, "MISSING"

    @staticmethod
    def _identities_compatible(
        source: Mapping[str, Any],
        normalized: Mapping[str, Any],
    ) -> bool:
        """Reject stale source-row matches when stable identity fields disagree."""
        source_symbol = canonical_symbol(source.get("symbol") or source.get("pair"))
        normalized_symbol = canonical_symbol(
            normalized.get("symbol") or normalized.get("pair")
        )
        if source_symbol and normalized_symbol and source_symbol != normalized_symbol:
            return False

        source_direction = str(
            source.get("direction") or source.get("side") or ""
        ).strip().upper()
        normalized_direction = str(
            normalized.get("direction") or normalized.get("side") or ""
        ).strip().upper()
        if (
            source_direction
            and normalized_direction
            and source_direction != normalized_direction
        ):
            return False

        for source_names, normalized_names in (
            (("opened_at", "open_timestamp", "timestamp"), ("opened_at",)),
            (("closed_at", "close_timestamp"), ("closed_at",)),
        ):
            source_value = next(
                (source.get(name) for name in source_names if source.get(name)), ""
            )
            normalized_value = next(
                (
                    normalized.get(name)
                    for name in normalized_names
                    if normalized.get(name)
                ),
                "",
            )
            source_time = canonical_timestamp(source_value)
            normalized_time = canonical_timestamp(normalized_value)
            if source_time and normalized_time and source_time != normalized_time:
                return False
        return True

    @staticmethod
    def similarity(target: Mapping[str, Any], candidate: Mapping[str, Any]) -> tuple[int, list[str]]:
        """Score similarity between current setup and historical trade."""
        score = 0
        reasons = []
        if target.get("symbol") == candidate.get("symbol"):
            score += 3
            reasons.append("same symbol")
        if target.get("direction") == candidate.get("direction"):
            score += 2
            reasons.append("same direction")
        if target.get("quality") == candidate.get("quality"):
            score += 1
            reasons.append("same quality")
        if abs(safe_float(target.get("confidence")) - safe_float(candidate.get("confidence"))) <= 10:
            score += 1
            reasons.append("similar confidence")
        if abs(safe_float(target.get("edge")) - safe_float(candidate.get("edge"))) <= 5:
            score += 1
            reasons.append("similar edge")
        if abs(safe_float(target.get("score")) - safe_float(candidate.get("score"))) <= 3:
            score += 1
            reasons.append("similar score")
        if TradeMemory.engine_status(target, "momentum") == TradeMemory.engine_status(candidate, "momentum"):
            score += 1
            reasons.append("same momentum status")
        return score, reasons

    @staticmethod
    def engine_status(decision: Mapping[str, Any], engine: str) -> str:
        """Derive engine PASS/FAIL for selected direction."""
        direction = str(decision.get("direction", ""))
        long_value = safe_float(decision.get(f"{engine}_long"))
        short_value = safe_float(decision.get(f"{engine}_short"))
        selected = long_value if direction == "LONG" else short_value
        opposite = short_value if direction == "LONG" else long_value
        return "PASS" if selected > 0 and selected >= opposite else "FAIL"

    @staticmethod
    def target_summary(target: Mapping[str, Any]) -> dict[str, Any]:
        """Return target setup summary."""
        return {
            "symbol": target.get("symbol", ""),
            "direction": target.get("direction", ""),
            "signal": target.get("signal", ""),
            "score": target.get("score", ""),
            "confidence": target.get("confidence", ""),
            "quality": target.get("quality", ""),
            "edge": target.get("edge", ""),
            "momentum": TradeMemory.engine_status(target, "momentum"),
        }

    @staticmethod
    def recommendation(matches: list[Mapping[str, Any]], stats: Mapping[str, Any]) -> str:
        """Return conservative recommendation."""
        if len(matches) < 5:
            return "Похожих сделок мало. Использовать только как контекст."
        if stats.get("profit_factor") is None:
            return (
                "Метрики похожих сделок недоступны: отсутствует надёжная связь "
                "с normalized metrics."
            )
        if safe_float(stats.get("profit_factor")) < 1:
            return "Похожие сделки исторически слабые. Новые входы требуют осторожности."
        return "Похожие сделки выглядят приемлемо, но решение не менять автоматически."

    @staticmethod
    def format_summary(report: Mapping[str, Any]) -> str:
        """Format Russian summary."""
        target = report.get("target", {})
        stats = report.get("stats", {})
        profit_factor = stats.get("profit_factor")
        metric_text = "недоступен" if profit_factor is None else str(profit_factor)
        lines = [
            "====================================",
            "Trade Memory v1",
            "====================================",
            f"Цель: {target.get('symbol')} {target.get('direction')}",
            f"Score: {target.get('score')} | Confidence: {target.get('confidence')} | Edge: {target.get('edge')}",
            f"Momentum: {target.get('momentum')}",
            f"Похожих сделок: {report.get('matches_count', 0)}",
            f"Winrate: {stats.get('winrate', 0)}%",
            f"Profit Factor: {metric_text}",
            f"Net R: {stats.get('net_r') if stats.get('net_r') is not None else 'недоступен'}",
            f"Средний R: {stats.get('average_r') if stats.get('average_r') is not None else 'недоступен'}",
            f"Max Drawdown: {stats.get('max_drawdown_r') if stats.get('max_drawdown_r') is not None else 'недоступен'} R",
            f"С полными метриками: {stats.get('metrics_complete_total', 0)}",
            f"Incomplete metrics: {stats.get('metrics_incomplete_total', 0)}",
            f"Не сопоставлено: {stats.get('unmatched_similar_trades', 0)}",
            f"Качество linkage: {stats.get('subset_match_quality', {})}",
            "Выборка метрик: только найденные похожие сделки.",
            f"Рекомендация: {report.get('recommendation')}",
        ]
        unavailable_reason = stats.get("profit_factor_unavailable_reason")
        if profit_factor is None and unavailable_reason:
            lines.insert(8, f"Причина: {unavailable_reason}")
        for warning in stats.get("data_quality_warnings", []):
            lines.append(str(warning))
        return "\n".join(lines)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Trade Memory")
    parser.add_argument("symbol", nargs="?", default=None)
    args = parser.parse_args()
    memory = TradeMemory()
    report = memory.build_report(args.symbol)
    print(memory.format_summary(report))


if __name__ == "__main__":
    main()
