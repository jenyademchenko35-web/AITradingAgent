"""Conservative confidence fusion for accepted research reports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


SOURCE_WEIGHTS = {
    "replay": 0.30,
    "lab": 0.25,
    "memory": 0.15,
    "loss": 0.15,
    "consensus": 0.15,
}


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _sample_size(payload: Mapping[str, Any]) -> int:
    metadata = payload.get("metadata", {})
    candidates = [
        metadata.get("complete_metrics_total") if isinstance(metadata, Mapping) else 0,
        payload.get("opportunities"),
        payload.get("matches_count"),
        payload.get("closed_trades"),
    ]
    for block_name in ("sample", "metrics", "stats", "baseline"):
        block = payload.get(block_name, {})
        if isinstance(block, Mapping):
            candidates.extend([
                block.get("complete_metrics_total"),
                block.get("metrics_trades"),
                block.get("closed_trades"),
                block.get("trades"),
            ])
    return max((int(_number(value)) for value in candidates), default=0)


def _quality_factor(payload: Mapping[str, Any]) -> float:
    status = str(payload.get("status") or "").upper()
    if status in {"OK", "READY", "SUPPORTED", "CURRENT"}:
        return 1.0
    if status in {"WARNING", "PARTIAL", "INSUFFICIENT_DATA", "OBSERVATION_ONLY"}:
        return 0.65
    if status in {"NO_DATA", "ERROR", "OFFLINE"}:
        return 0.2
    return 0.75


@dataclass(frozen=True)
class SourceFinding:
    """One normalized research conclusion."""

    source: str
    finding: str
    candidate: str
    sample_size: int
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "finding": self.finding,
            "candidate": self.candidate,
            "sample_size": self.sample_size,
            "confidence": round(self.confidence, 4),
        }


class AdaptiveRecommendationEngine:
    """Merge evidence without producing automatic strategy actions."""

    def build(
        self,
        payloads: Mapping[str, Mapping[str, Any]],
        closed_trades: int,
    ) -> dict[str, Any]:
        findings = [
            finding
            for key, payload in payloads.items()
            if key in SOURCE_WEIGHTS
            for finding in [self._finding(key, payload)]
            if finding is not None
        ]
        contributions = []
        global_confidence = 0.0
        for key, weight in SOURCE_WEIGHTS.items():
            finding = next((item for item in findings if item.source == key), None)
            confidence = finding.confidence if finding else 0.0
            contribution = weight * confidence
            global_confidence += contribution
            contributions.append({
                "source": key,
                "weight": weight,
                "confidence": round(confidence, 4),
                "contribution": round(contribution, 4),
                "accepted": finding is not None,
            })

        candidate_counts: dict[str, int] = {}
        for finding in findings:
            if finding.candidate:
                candidate_counts[finding.candidate] = (
                    candidate_counts.get(finding.candidate, 0) + 1
                )
        agreement = max(candidate_counts.values(), default=0)
        leader = (
            max(candidate_counts, key=candidate_counts.get, default="")
            if agreement >= 2
            else ""
        )

        if closed_trades < 50:
            status = "COLLECT_MORE_DATA"
            primary = "Продолжить Shadow Research до минимум 50 закрытых сделок."
        elif global_confidence < 0.60 or agreement < 2:
            status = "CONTINUE_SHADOW"
            primary = "Продолжить независимую проверку гипотез в shadow/replay."
        else:
            status = "READY_FOR_MANUAL_REVIEW"
            primary = (
                "Доказательств достаточно только для ручного review; "
                "автоматическое применение запрещено."
            )

        return {
            "status": status,
            "global_confidence": round(global_confidence, 4),
            "global_confidence_percent": round(global_confidence * 100, 2),
            "leader": leader or "Нет согласованной гипотезы",
            "agreement_modules": agreement,
            "primary": primary,
            "findings": [finding.to_dict() for finding in findings],
            "confidence_fusion": contributions,
            "forbidden_actions": [
                "Не менять DecisionEngine автоматически.",
                "Не писать параметры в config.py.",
                "Не менять Entry/Exit, SL/TP/RR и Risk.",
                "Не применять гипотезы без ручного подтверждения.",
            ],
        }

    def _finding(
        self,
        key: str,
        payload: Mapping[str, Any],
    ) -> SourceFinding | None:
        sample = _sample_size(payload)
        confidence = min(sample / 50.0, 1.0) * _quality_factor(payload)
        candidate = ""
        finding = ""

        if key == "replay":
            metrics = payload.get("metrics", {})
            effective = metrics.get("effective_portfolio", {}) if isinstance(metrics, Mapping) else {}
            finding = (
                f"Effective PF={_number(effective.get('profit_factor')):.3f}; "
                f"Net R={_number(effective.get('net_r')):+.3f}. "
                f"{payload.get('recommendation', '')}"
            ).strip()
            candidate = "Execution Realism"
        elif key == "lab":
            leader = payload.get("leader")
            if isinstance(leader, Mapping):
                candidate = str(leader.get("hypothesis") or "")
            if not candidate:
                ranking = payload.get("ranking", [])
                if isinstance(ranking, list):
                    first = next(
                        (
                            item for item in ranking
                            if isinstance(item, Mapping)
                            and _number(item.get("trades")) > 0
                        ),
                        {},
                    )
                    candidate = str(first.get("hypothesis") or "")
            finding = str(payload.get("recommendation") or "Нет рекомендации Strategy Lab.")
        elif key == "memory":
            target = payload.get("target", {})
            if isinstance(target, Mapping):
                candidate = str(target.get("symbol") or target.get("name") or "")
            finding = str(payload.get("recommendation") or "Trade Memory: продолжить сбор данных.")
        elif key == "loss":
            patterns = payload.get("patterns", [])
            if isinstance(patterns, list) and patterns:
                first = patterns[0] if isinstance(patterns[0], Mapping) else {}
                candidate = str(first.get("pattern") or "")
            finding = str(payload.get("conclusion") or "Loss Analyzer: закономерность не доказана.")
        elif key == "consensus":
            ranking = payload.get("ranking", [])
            if isinstance(ranking, list) and ranking:
                first = ranking[0] if isinstance(ranking[0], Mapping) else {}
                candidate = str(first.get("hypothesis") or first.get("name") or "")
                verdict = str(first.get("verdict") or "")
                finding = f"{candidate or 'Гипотеза'}: {verdict or 'нет verdict'}."
            else:
                finding = "Research Consensus: недостаточно данных."

        if not finding:
            return None
        return SourceFinding(
            source=key,
            finding=" ".join(finding.split()),
            candidate=candidate,
            sample_size=sample,
            confidence=round(confidence, 4),
        )
