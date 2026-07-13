"""Detect and classify conflicting research evidence."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Iterable

from research_orchestrator.models import Conflict, Evidence


class ConflictResolver:
    """Build explicit Lab/Replay/Memory conflicts instead of hiding them."""

    def resolve(self, evidence: Iterable[Evidence]) -> list[Conflict]:
        grouped: dict[str, list[Evidence]] = defaultdict(list)
        for item in evidence:
            if item.freshness_status == "VALID" and item.quality_status == "VALID":
                grouped[item.hypothesis].append(item)

        conflicts = []
        for hypothesis, rows in grouped.items():
            supporting = sorted({
                item.source for item in rows if item.direction == "SUPPORTS"
            })
            contradicting = sorted({
                item.source for item in rows if item.direction == "CONTRADICTS"
            })
            if not supporting or not contradicting:
                continue
            severity = self._severity(rows, supporting, contradicting)
            resolution = self._resolution(rows, contradicting)
            digest = hashlib.sha1(hypothesis.encode("utf-8")).hexdigest()[:12]
            conflicts.append(Conflict(
                conflict_id=f"CF-{digest}",
                hypothesis=hypothesis,
                supporting_sources=tuple(supporting),
                contradicting_sources=tuple(contradicting),
                severity=severity,
                resolution=resolution,
                action=self._action(resolution, hypothesis),
            ))
        return conflicts

    @staticmethod
    def _severity(
        rows: list[Evidence],
        supporting: list[str],
        contradicting: list[str],
    ) -> str:
        if any(
            item.category == "DATA_QUALITY" and "CRITICAL" in item.notes.upper()
            for item in rows
        ):
            return "CRITICAL"
        if len(supporting) >= 2 and len(contradicting) >= 2:
            return "HIGH"
        if len(supporting) + len(contradicting) >= 3:
            return "MEDIUM"
        return "LOW"

    @staticmethod
    def _resolution(rows: list[Evidence], contradicting: list[str]) -> str:
        if any(item.category == "DATA_QUALITY" for item in rows):
            return "DATA_QUALITY_FIX"
        if any("replay" in source.lower() for source in contradicting):
            return "RUN_WALK_FORWARD"
        if any("stale" in item.notes.lower() for item in rows):
            return "REFRESH_REPORT"
        return "WAIT_FOR_MORE_DATA"

    @staticmethod
    def _action(resolution: str, hypothesis: str) -> str:
        actions = {
            "DATA_QUALITY_FIX": "Сначала устранить проблему качества данных.",
            "RUN_WALK_FORWARD": f"Запустить walk-forward для {hypothesis}.",
            "REFRESH_REPORT": "Обновить устаревшие read-only отчёты.",
            "WAIT_FOR_MORE_DATA": "Продолжить сбор независимых наблюдений.",
        }
        return actions.get(resolution, "Не переносить гипотезу в LIVE.")
