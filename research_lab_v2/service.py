"""Cycle-level orchestration for the research database."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from strategies import registry

from .analytics import calculate_metrics, feature_importance, promotion_decision, rank_strategies
from .database import ResearchDatabase


class ResearchLab:
    def __init__(self, database_path: str | Path = "research.db", *, ranking_interval: int = 10,
                 feature_interval: int = 100) -> None:
        self.database = ResearchDatabase(database_path)
        self.ranking_interval = max(1, int(ranking_interval))
        self.feature_interval = max(1, int(feature_interval))

    def register_strategies(self) -> None:
        for strategy in registry.all():
            self.database.upsert_strategy(strategy)

    def process_cycle(self, *, cycle_id: str, snapshot: Mapping[str, Any],
                      decisions: Iterable[Mapping[str, Any]],
                      closed_trades: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
        self.register_strategies()
        decisions, closed = list(decisions), list(closed_trades)
        timestamp = str(snapshot.get("timestamp", ""))
        symbol = str(snapshot.get("symbol", ""))
        for decision in decisions:
            strategy_id = str(decision.get("candidate_id", "")).upper()
            if not strategy_id or registry.get(strategy_id) is None:
                continue
            features = decision.get("feature_snapshot")
            features = features if isinstance(features, Mapping) else snapshot
            self.database.record_run(
                cycle_id=cycle_id, strategy_id=strategy_id, timestamp=timestamp,
                symbol=symbol, timeframe=str(snapshot.get("timeframe", "1h")),
                decision=str(decision.get("decision", "")),
                status=str(decision.get("status", "EVALUATED")), features=features,
                shadow_trade_id=decision.get("shadow_trade_id"),
                would_open_trade=bool(decision.get("would_open_trade", False)),
                block_reason=decision.get("block_reason"),
            )
        for trade in closed:
            strategy_id = str(trade.get("candidate_id", "")).upper()
            if registry.get(strategy_id) is None:
                continue
            features = trade.get("feature_snapshot")
            features = features if isinstance(features, Mapping) else {}
            trade_id = str(trade.get("shadow_trade_id", ""))
            self.database.record_run(
                cycle_id=f"{cycle_id}:closed:{trade_id}", strategy_id=strategy_id,
                timestamp=str(trade.get("closed_at", timestamp)),
                symbol=str(trade.get("symbol", symbol)),
                timeframe=str(trade.get("timeframe", snapshot.get("timeframe", "1h"))),
                decision="CLOSED",
                status=str(trade.get("status", "CLOSED")), features=features,
                result_r=float(trade.get("pnl_r", 0) or 0), shadow_trade_id=trade_id,
            )
        if not closed:
            return {"runs": len(decisions), "closed": 0, "ranked": False}

        grouped = self.database.completed_runs()
        metrics_rows = []
        walk_forward = self.database.latest_walk_forward()
        for strategy_id, trades in grouped.items():
            metrics = calculate_metrics(float(row.get("pnl_r", 0) or 0) for row in trades)
            wf = walk_forward.get(strategy_id, {})
            metrics_rows.append({
                "strategy_id": strategy_id, **metrics,
                "walk_forward_status": wf.get("status", "NOT_RUN"),
                "better_windows": wf.get("better_windows", 0),
                "profitable_windows": wf.get("profitable_windows", 0),
                "confidence": wf.get("confidence", "LOW"),
            })
            self.database.record_metrics(strategy_id, metrics)
            if metrics["closed_trades"] % self.feature_interval == 0:
                stats = feature_importance([
                    {**dict(row.get("feature_snapshot", {})), "pnl_r": row.get("pnl_r", 0)}
                    for row in trades
                ])
                self.database.record_feature_statistics(strategy_id, stats)
        if self.database.cycle_count() % self.ranking_interval:
            return {"runs": len(decisions), "closed": len(closed), "ranked": False}
        ranking = rank_strategies(metrics_rows)
        baseline = next((row for row in ranking if row["strategy_id"] == "LIVE_BASELINE"), {})
        for row in ranking:
            if row["strategy_id"] == "LIVE_BASELINE":
                continue
            self.database.record_candidate(row["strategy_id"], promotion_decision(row, baseline))
        return {"runs": len(decisions), "closed": len(closed), "ranked": True, "ranking": ranking}

    def record_walk_forward_report(self, report: Mapping[str, Any]) -> None:
        configuration = report.get("configuration", {})
        candidate = report.get("candidate", {})
        comparison = report.get("comparison", {})
        strategy_id = str(configuration.get("candidate_id", "")).upper()
        if not strategy_id or registry.get(strategy_id) is None:
            raise ValueError("walk-forward report has no registered candidate_id")
        self.register_strategies()
        self.database.record_walk_forward(strategy_id, {
            "generated_at": report.get("generated_at"),
            "status": "PASS" if int(candidate.get("windows", 0) or 0) >= 3 else report.get("status", "NOT_RUN"),
            "windows": int(candidate.get("windows", 0) or 0),
            "better_windows": int(comparison.get("candidate_better_windows", 0) or 0),
            "profitable_windows": int(candidate.get("profitable_windows", 0) or 0),
            "oos_pf": candidate.get("profit_factor"), "oos_net_r": candidate.get("net_r", 0),
            "confidence": report.get("bootstrap", {}).get("confidence", "LOW"),
        })
