"""Read-only Research Dashboard v2 projection and Telegram-friendly views."""

from __future__ import annotations

import json
import sqlite3
import csv
from pathlib import Path
from typing import Any

from .runtime import SHADOW_BOOK_FILE, SHADOW_HISTORY_FILE, load_runtime_status


def _rows(path: Path, query: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query)]
    except sqlite3.Error:
        return []
    finally:
        connection.close()


class ResearchDashboardV2:
    def __init__(self, database_path: str | Path = "research.db", *,
                 status_path: str | Path | None = None,
                 shadow_book_path: str | Path = SHADOW_BOOK_FILE,
                 shadow_history_path: str | Path = SHADOW_HISTORY_FILE) -> None:
        self.path = Path(database_path)
        self.status_path = Path(status_path) if status_path is not None else None
        self.shadow_book_path = Path(shadow_book_path)
        self.shadow_history_path = Path(shadow_history_path)

    def _shadow_ledger(self) -> dict[str, list[dict[str, Any]]]:
        try:
            payload = json.loads(self.shadow_book_path.read_text(encoding="utf-8"))
            open_rows = payload if isinstance(payload, list) else []
        except (OSError, ValueError, TypeError):
            open_rows = []
        try:
            with self.shadow_history_path.open("r", encoding="utf-8", newline="") as handle:
                closed_rows = list(csv.DictReader(handle))
        except OSError:
            closed_rows = []
        return {"open": open_rows, "closed": closed_rows}

    def build_report(self) -> dict[str, Any]:
        top = _rows(self.path, """
            WITH latest AS (
              SELECT strategy_id, MAX(id) id FROM strategy_metrics GROUP BY strategy_id
            ), wf AS (
              SELECT strategy_id, MAX(id) id FROM walk_forward_results GROUP BY strategy_id
            ), history AS (
              SELECT strategy_id, MAX(id) id FROM candidate_history GROUP BY strategy_id
            )
            SELECT m.strategy_id, s.name, m.profit_factor, m.winrate, m.net_r,
                   m.max_drawdown, m.sharpe, m.sortino, m.expectancy, m.final_score,
                   COALESCE(w.status, 'NOT_RUN') walk_forward,
                   COALESCE(w.confidence, 'LOW') confidence,
                   COALESCE(h.status, 'RESEARCH') status,
                   COALESCE(h.promotion_probability, 0) promotion_probability,
                   m.closed_trades
            FROM latest l JOIN strategy_metrics m ON m.id=l.id
            JOIN strategies s ON s.id=m.strategy_id
            LEFT JOIN wf x ON x.strategy_id=m.strategy_id LEFT JOIN walk_forward_results w ON w.id=x.id
            LEFT JOIN history y ON y.strategy_id=m.strategy_id LEFT JOIN candidate_history h ON h.id=y.id
            ORDER BY m.final_score DESC, m.strategy_id LIMIT 20
        """)
        for index, row in enumerate(top, 1):
            row["rank"] = index
        from .candidate_policy import rejected_decision
        for row in top:
            rejected = rejected_decision(row["strategy_id"])
            if rejected:
                row.update(rejected)
        if top and not any(row["strategy_id"] == "MOMENTUM_RELAXED" for row in top):
            rejected = rejected_decision("MOMENTUM_RELAXED")
            if rejected:
                top.append({"strategy_id": "MOMENTUM_RELAXED", "name": "Momentum Relaxed",
                            "rank": None, "walk_forward": "REJECTED", "confidence": "HIGH",
                            "winrate": None, "max_drawdown": None, "closed_trades": None,
                            **rejected})
        features = _rows(self.path, """
            WITH latest AS (SELECT strategy_id, MAX(calculated_at) stamp FROM feature_statistics GROUP BY strategy_id)
            SELECT f.* FROM feature_statistics f JOIN latest l
              ON l.strategy_id=f.strategy_id AND l.stamp=f.calculated_at
            ORDER BY ABS(f.importance) DESC LIMIT 30
        """)
        positive = [row for row in features if row["importance"] > 0][:5]
        negative = [row for row in features if row["importance"] < 0][:5]
        best = next((row for row in top if row.get("status") != "REJECTED"), {})
        strategies = _rows(self.path, "SELECT id, name, version, enabled, risk_profile, shadow_only FROM strategies ORDER BY id")
        run_count = _rows(self.path, "SELECT COUNT(*) count FROM strategy_runs")
        return {
            "top_strategies": top, "top_features": positive, "worst_features": negative,
            "research_progress": {
                "registered_strategies": len(strategies),
                "strategy_runs": run_count[0]["count"] if run_count else 0,
                "ranked_strategies": len(top),
            },
            "best_candidate": best,
            "promotion_probability": best.get("promotion_probability", 0),
            "strategies": strategies,
            "runtime_status": (
                load_runtime_status(self.status_path)
                if self.status_path is not None else load_runtime_status()
            ),
            "shadow_ledger": self._shadow_ledger(),
        }

    def format(self, section: str = "top") -> str:
        report = self.build_report()
        section = section.lower()
        if section in {"top", "research_rank"}:
            lines = ["Research Lab v2 - TOP STRATEGIES"]
            for row in report["top_strategies"][:10]:
                lines.append(
                    f"{row['rank']}. {row['strategy_id']} | PF {row['profit_factor']} | "
                    f"WR {row['winrate'] if row.get('winrate') is not None else 'N/A'} | NetR {row.get('net_r', 'N/A')} | "
                    f"WF {row['walk_forward']} | {row['confidence']} | {row['status']}"
                )
            return "\n".join(lines + (["No ranked strategies."] if len(lines) == 1 else []))
        if section == "features":
            return "\n".join([
                "Research Lab v2 - FEATURES",
                "Top: " + ", ".join(row["feature_name"] for row in report["top_features"]) or "Top: N/A",
                "Worst: " + ", ".join(row["feature_name"] for row in report["worst_features"]) or "Worst: N/A",
            ])
        if section == "strategies":
            return "\n".join(["Research Lab v2 - STRATEGIES"] + [
                f"{row['id']} v{row['version']} | {'ON' if row['enabled'] else 'OFF'} | "
                f"{row['risk_profile']} | {'SHADOW' if row['shadow_only'] else 'LIVE METADATA'}"
                for row in report["strategies"]
            ])
        if section == "researchlab":
            runtime = report["runtime_status"]
            blocked = runtime.get("blocked", {})
            lines = [
                "Research Lab v2 - RUNTIME",
                f"Enabled: {'ON' if runtime.get('enabled') else 'OFF'}",
                f"Dry Run: {'ON' if runtime.get('dry_run') else 'OFF'}",
                f"Real Orders Allowed: {'YES' if runtime.get('real_order_allowed') else 'NO'}",
                "Allowlist: " + ", ".join(runtime.get("strategies_enabled", [])),
                "Strategy Modes:",
                *[
                    f"- {strategy_id}: {mode}"
                    for strategy_id, mode in sorted(runtime.get("strategy_modes", {}).items())
                ],
                f"Last Cycle: {runtime.get('last_processed_cycle', 'NEVER')}",
                f"Evaluations: {runtime.get('runs_this_cycle', 0)}",
                f"New Entry Triggers: {runtime.get('new_entry_triggers', 0)}",
                f"Would Open: {runtime.get('would_open', 0)}",
                f"Opened Shadow: {runtime.get('opened_shadow', 0)}",
                f"Open Research Shadow: {runtime.get('open_research_shadow_trades', 0)}",
                f"Closed Research Shadow: {runtime.get('closed_research_shadow_trades', 0)}",
                "Open Per Strategy: " + (", ".join(
                    f"{key}={value}" for key, value in sorted(
                        runtime.get("open_per_strategy", {}).items()
                    )
                ) or "none"),
                f"Shadow Mode Started: {runtime.get('shadow_mode_started_at') or 'NOT_STARTED'}",
                "Last Opened: " + self._trade_summary(runtime.get("last_opened")),
                "Last Closed: " + self._trade_summary(runtime.get("last_closed")),
                "Blocked: " + (", ".join(f"{key}={value}" for key, value in blocked.items()) or "none"),
                f"DB: {runtime.get('database_status', 'NOT_INITIALIZED')}",
                f"Last Error: {runtime.get('last_error') or 'none'}",
            ]
            diagnostics = runtime.get("dry_run_diagnostics", {})
            for strategy_id in sorted(diagnostics):
                item = diagnostics[strategy_id]
                reasons = item.get("blocked_by_reason", {})
                lines.extend([
                    "",
                    strategy_id,
                    f"Evaluations: {item.get('evaluations', 0)}",
                    f"Condition Active: {item.get('condition_active', 0)}",
                    f"New Entry Triggers: {item.get('new_entry_triggers', 0)}",
                    f"Repeated Active: {item.get('repeated_active_conditions', 0)}",
                    f"Would Open: {item.get('would_open', 0)}",
                    f"Actual Shadow Opened: {item.get('actual_shadow_opened', 0)}",
                    f"Signal Rate: {item.get('signal_rate', 0):.2f}%",
                    f"Unique Fingerprints: {item.get('unique_signal_fingerprints', 0)}",
                    "Symbols: " + (", ".join(item.get("symbols_with_signals", [])) or "none"),
                    "Blocked: " + (", ".join(
                        f"{reason}={count}" for reason, count in sorted(reasons.items())
                    ) or "none"),
                ])
            return "\n".join(lines)
        if section == "researchlab_trades":
            ledger = report["shadow_ledger"]
            lines = [
                "Research Lab v2 - SHADOW TRADES",
                f"Open: {len(ledger['open'])}",
                f"Closed: {len(ledger['closed'])}",
                "",
                "OPEN",
            ]
            lines.extend(self._trade_summary(row) for row in ledger["open"])
            if not ledger["open"]:
                lines.append("none")
            lines.extend(["", "LAST CLOSED"])
            lines.extend(self._trade_summary(row) for row in ledger["closed"][-10:])
            if not ledger["closed"]:
                lines.append("none")
            return "\n".join(lines)
        if section == "promotions":
            return "\n".join(["Research Lab v2 - PROMOTIONS"] + [
                f"{row['strategy_id']}: {row['status']} ({row['promotion_probability']:.1f}%)"
                for row in report["top_strategies"] if row["strategy_id"] != "LIVE_BASELINE"
            ])
        return json.dumps(report, ensure_ascii=False, indent=2, default=str)

    @staticmethod
    def _trade_summary(trade: Any) -> str:
        if not isinstance(trade, dict):
            return "none"
        return (
            f"{trade.get('shadow_trade_id', 'N/A')} | "
            f"{trade.get('strategy_id', 'N/A')} | {trade.get('symbol', 'N/A')} "
            f"{trade.get('side') or trade.get('direction', 'N/A')} | "
            f"{trade.get('status', 'N/A')} | "
            f"exit={trade.get('exit_reason', 'OPEN')} | pnl={trade.get('pnl_r', 'N/A')}R"
        )
