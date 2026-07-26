"""Read-only attribution of recently blocked agent decisions."""

from __future__ import annotations

import csv
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

BASE_DIR = Path(__file__).resolve().parent


class RootCauseAnalyzer:
    """Build a Telegram-safe ranking from existing decision artifacts."""

    def __init__(self, base_dir: str | Path = BASE_DIR) -> None:
        self.base_dir = Path(base_dir)

    @staticmethod
    def _normalize(value: Any) -> str:
        text = re.sub(r"[_-]+", " ", str(value or "")).strip()
        compact = re.sub(r"\s+", " ", text).lower()
        aliases = {
            "trend": "Trend",
            "momentum": "Momentum",
            "structure": "Structure",
            "risk": "Risk",
            "cooldown": "Cooldown",
            "blocked cooldown": "Cooldown",
            "missing data": "Data Quality",
            "data quality": "Data Quality",
        }
        return aliases.get(compact, text.title() if text else "")

    @staticmethod
    def _is_blocked(row: Mapping[str, Any]) -> bool:
        values = " ".join(
            str(row.get(key, "")) for key in
            ("decision", "signal", "live_decision", "status", "live_status")
        ).upper()
        if any(token in values for token in ("NO TRADE", "BLOCKED", "REJECTED")):
            return True
        # Old decision_diagnostics rows record module failures and a blocker.
        return bool(row.get("primary_blocker")) and any(
            str(row.get(key, "")).upper() == "FAIL"
            for key in ("trend", "momentum", "structure", "risk")
        )

    def _rows(self) -> list[dict[str, str]]:
        paths = (
            self.base_dir / "decision_diagnostics.csv",
            self.base_dir / "decision_features.csv",
        )
        for path in paths:
            try:
                with path.open(newline="", encoding="utf-8") as stream:
                    rows = [dict(row) for row in csv.DictReader(stream)]
                if rows:
                    return rows
            except (OSError, csv.Error, UnicodeError):
                continue
        return []

    @staticmethod
    def _causes(row: Mapping[str, Any]) -> Iterable[str]:
        for field in ("primary_blocker", "secondary_blocker", "blocker", "reason"):
            value = str(row.get(field, "") or "").strip()
            if value:
                yield value

    def build_report(self, limit: int = 1000) -> dict[str, Any]:
        period_limit = max(1, int(limit))
        rows = self._rows()[-period_limit:]
        blocked_rows = [row for row in rows if self._is_blocked(row)]
        counts: Counter[str] = Counter()
        for row in blocked_rows:
            seen: set[str] = set()
            for raw_cause in self._causes(row):
                cause = self._normalize(raw_cause)
                if cause and cause.lower() not in {"none", "unknown", "n/a"}:
                    seen.add(cause)
            counts.update(seen)
        blocked = len(blocked_rows)
        causes = []
        for name, count in counts.most_common(8):
            percentage = count / blocked * 100 if blocked else 0.0
            severity = "HIGH" if percentage >= 30 else "MEDIUM" if percentage >= 15 else "LOW"
            causes.append({
                "name": name, "count": count,
                "percentage": round(percentage, 2), "severity": severity,
            })
        top = causes[0]["name"] if causes else None
        return {
            "status": "OK" if blocked and causes else "INSUFFICIENT_DATA",
            "period_limit": period_limit,
            "analyzed": len(rows),
            "blocked": blocked,
            "causes": causes,
            "top_cause": top,
            "summary": f"{top} является основным ограничителем входов." if top else "",
        }

    def format_telegram(self, report: Mapping[str, Any]) -> str:
        if report.get("status") != "OK":
            return "🧠 Root Cause Report\nНедостаточно данных для анализа."
        icons = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟡"}
        lines = [
            "🧠 Root Cause Report", "",
            f"Период: последние {report.get('period_limit', 1000)} решений",
            f"Проанализировано: {report.get('analyzed', 0)}",
            f"Заблокировано: {report.get('blocked', 0)}", "", "Причины:",
        ]
        for index, cause in enumerate(report.get("causes", [])[:8], 1):
            severity = str(cause["severity"])
            lines.append(
                f"{index}. {cause['name']} — {cause['count']} "
                f"({cause['percentage']:.1f}%) {icons.get(severity, '🟡')} {severity}"
            )
        lines.extend([
            "", f"Главная причина: {report.get('top_cause')}",
            f"Вывод: {report.get('summary')}",
        ])
        return "\n".join(lines)[:4096]
