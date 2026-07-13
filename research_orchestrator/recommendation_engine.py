"""Conservative next-action selection for Research Orchestrator."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from research_orchestrator.models import Conflict, HypothesisAssessment


ALLOWED_RECOMMENDATIONS = frozenset({
    "CONTINUE_COLLECTION",
    "REFRESH_STALE_REPORTS",
    "RUN_SHADOW_REPLAY",
    "RUN_WALK_FORWARD",
    "INVESTIGATE_DATA_QUALITY",
    "TEST_ONE_HYPOTHESIS",
    "KEEP_LIVE_UNCHANGED",
    "REVIEW_ENTRY_TIMING",
    "REVIEW_STOP_ZONE",
    "REVIEW_MOMENTUM_FILTER",
    "REVIEW_SYMBOL_SPECIFIC_EDGE",
})


class RecommendationEngine:
    """Choose research work without issuing any trading instruction."""

    STATUS_PRIORITY = {
        "STRONG_CANDIDATE": 6,
        "PROMISING_CANDIDATE": 5,
        "OBSERVATION_ONLY": 4,
        "INSUFFICIENT_DATA": 3,
        "CONFLICTED": 2,
        "DATA_QUALITY_BLOCKED": 1,
        "REJECTED": 0,
        "OVERFILTERED": -1,
    }

    def select_leader(
        self,
        assessments: Iterable[HypothesisAssessment],
    ) -> HypothesisAssessment | None:
        eligible = [
            item for item in assessments
            if item.status not in {"OVERFILTERED", "REJECTED", "DATA_QUALITY_BLOCKED"}
            and int(float(item.metrics.get("trades", 0) or 0)) > 0
        ]
        if not eligible:
            return None
        return max(eligible, key=lambda item: (
            self.STATUS_PRIORITY.get(item.status, -2),
            len(item.supporting_sources),
            float(item.metrics.get("profit_factor", 0) or 0),
            float(item.metrics.get("net_r", item.metrics.get("roi", 0)) or 0),
        ))

    def build(
        self,
        assessments: Iterable[HypothesisAssessment],
        conflicts: Iterable[Conflict],
        diagnostics: Mapping[str, Any],
        *,
        critical_data_quality: bool = False,
    ) -> dict[str, Any]:
        rows = list(assessments)
        conflict_rows = list(conflicts)
        leader = self.select_leader(rows)
        stale_count = len(diagnostics.get("stale_artifacts", []))
        legacy_count = len(diagnostics.get("legacy_artifacts", []))

        if critical_data_quality:
            next_action = "INVESTIGATE_DATA_QUALITY"
            next_experiment = "Исправить CRITICAL data-quality warning."
        elif stale_count:
            next_action = "REFRESH_STALE_REPORTS"
            next_experiment = "Обновить только безопасные read-only отчёты."
        elif conflict_rows:
            next_action = "RUN_WALK_FORWARD"
            next_experiment = f"RUN_WALK_FORWARD для {conflict_rows[0].hypothesis}."
        elif leader and leader.status in {"PROMISING_CANDIDATE", "STRONG_CANDIDATE"}:
            next_action = "TEST_ONE_HYPOTHESIS"
            next_experiment = f"Изолированный shadow-тест: {leader.hypothesis}."
        elif legacy_count:
            next_action = "REFRESH_STALE_REPORTS"
            next_experiment = "Пересоздать legacy-отчёты с canonical metadata."
        else:
            next_action = "CONTINUE_COLLECTION"
            next_experiment = "Продолжить накопление закрытых сделок и Dry-run."

        return {
            "primary": "KEEP_LIVE_UNCHANGED",
            "next_action": next_action,
            "next_experiment": next_experiment,
            "leader": leader.hypothesis if leader else "Нет подтверждённого кандидата",
            "leader_status": leader.status if leader else "INSUFFICIENT_DATA",
            "leader_confidence": leader.confidence if leader else "VERY_LOW",
            "allowed_actions": ["KEEP_LIVE_UNCHANGED", next_action],
            "automatic_live_change": False,
        }
