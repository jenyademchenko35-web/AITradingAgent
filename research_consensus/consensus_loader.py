"""Safe loader for existing AITradingAgent research reports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
REPORT_FILES = {
    "trade_loss": "trade_loss_report.json",
    "trade_replay": "trade_replay_report.json",
    "strategy_lab": "strategy_lab_report.json",
    "hypothesis_lab": "hypothesis_report.json",
    "trade_memory": "trade_memory_report.json",
    "market_intelligence": "market_intelligence_report.json",
    "news_statistics": "news_statistics_report.json",
    "market_regime": "market_regime_advisor_report.json",
    "market_heatmap": "market_heatmap_report.json",
    "dashboard": "dashboard_state.json",
}


class ConsensusLoader:
    """Load reports without invoking their generators or external APIs."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.reports: dict[str, dict[str, Any]] = {}
        self.source_status: dict[str, dict[str, Any]] = {}
        self.warnings: list[str] = []
        self.load_all()

    def load_all(self) -> dict[str, dict[str, Any]]:
        """Read every configured report exactly once."""
        for key, filename in REPORT_FILES.items():
            path = self.base_dir / filename
            report, error = self.read_report(path)
            self.reports[key] = report
            self.source_status[key] = {
                "file": filename,
                "exists": path.exists(),
                "valid": bool(report),
                "generated_at": report.get("generated_at", "") if report else "",
                "modified_at": self.modified_at(path),
                "size_bytes": path.stat().st_size if path.exists() else 0,
                "error": error,
            }
            if error and path.exists():
                self.warnings.append(f"{filename}: {error}")
        return self.reports

    @staticmethod
    def read_report(path: Path) -> tuple[dict[str, Any], str]:
        """Read one JSON object and return a short error when invalid."""
        if not path.exists():
            return {}, "отчёт отсутствует"
        if path.stat().st_size == 0:
            return {}, "отчёт пуст"
        try:
            with path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            return {}, f"ошибка чтения JSON: {type(exc).__name__}"
        if not isinstance(payload, dict):
            return {}, "корневое значение JSON не является объектом"
        return payload, ""

    @staticmethod
    def modified_at(path: Path) -> str:
        """Return filesystem modification time in UTC."""
        if not path.exists():
            return ""
        return datetime.fromtimestamp(
            path.stat().st_mtime,
            tz=timezone.utc,
        ).isoformat()

    def closed_trades(self) -> int:
        """Return the largest reported closed-trade sample without summing duplicates."""
        candidates = [
            self.reports.get("trade_replay", {}).get("sample", {}).get("closed_trades", 0),
            self.reports.get("trade_loss", {}).get("sample", {}).get("closed_trades", 0),
            self.reports.get("news_statistics", {}).get("sample_size", 0),
            self.reports.get("market_intelligence", {}).get("trades", {}).get("closed_trades", 0),
            self.reports.get("dashboard", {}).get("trading", {}).get("closed_trades", 0),
        ]
        return max((self.safe_int(value) for value in candidates), default=0)

    def context(self) -> dict[str, Any]:
        """Return non-voting system context from aggregate/current-state reports."""
        intelligence = self.reports.get("market_intelligence", {})
        dashboard = self.reports.get("dashboard", {})
        heatmap = self.reports.get("market_heatmap", {})
        return {
            "system_status": dashboard.get("system", {}).get("status", "UNKNOWN"),
            "market_regime": intelligence.get("market", {}).get("regime", "Недостаточно данных"),
            "news_sentiment": intelligence.get("market", {}).get("news_sentiment", "Neutral"),
            "heatmap_status": heatmap.get("status", "NO_DATA"),
            "near_setup": heatmap.get("summary", {}).get("NEAR SETUP", 0),
            "note": (
                "Market Intelligence, Heatmap и Dashboard используются как контекст "
                "и не дублируют голоса первичных исследований."
            ),
        }

    @staticmethod
    def safe_int(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0
