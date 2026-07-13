"""Typed data contracts used by Research Orchestrator."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


ARTIFACT_STATUSES = frozenset({
    "VALID",
    "STALE",
    "MISSING",
    "INCOMPATIBLE",
    "CORRUPTED",
    "LEGACY",
    "EMPTY",
})

EVIDENCE_DIRECTIONS = frozenset({
    "SUPPORTS",
    "CONTRADICTS",
    "NEUTRAL",
    "INSUFFICIENT",
})

HYPOTHESIS_STATUSES = frozenset({
    "REJECTED",
    "OVERFILTERED",
    "INSUFFICIENT_DATA",
    "OBSERVATION_ONLY",
    "PROMISING_CANDIDATE",
    "STRONG_CANDIDATE",
    "CONFLICTED",
    "DATA_QUALITY_BLOCKED",
})


@dataclass(frozen=True)
class ArtifactSpec:
    """Static registry entry for one report artifact."""

    artifact_name: str
    report_type: str
    path: str
    required: bool
    schema_version: str
    metric_units: tuple[str, ...]
    freshness_ttl_hours: float
    owner_module: str
    sample_bound: bool = False

    @property
    def metric_unit(self) -> str:
        """Return the primary expected unit for registry displays."""
        return self.metric_units[0] if self.metric_units else "UNSPECIFIED"

    @property
    def freshness_ttl(self) -> float:
        """Return TTL in hours using the field name from the public contract."""
        return self.freshness_ttl_hours


@dataclass
class ArtifactRecord:
    """Loaded artifact plus freshness and compatibility diagnostics."""

    spec: ArtifactSpec
    resolved_path: Path
    status: str
    reasons: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    generated_at: str = ""
    modified_at: str = ""
    sample_size: int = 0
    source_hash: Any = ""
    age_hours: float | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "VALID"

    def to_dict(self, *, include_payload: bool = False) -> dict[str, Any]:
        result = {
            "artifact_name": self.spec.artifact_name,
            "report_type": self.spec.report_type,
            "path": self.spec.path,
            "required": self.spec.required,
            "schema_version": self.metadata.get("schema_version", ""),
            "metric_unit": self.metadata.get("metric_unit", ""),
            "generated_at": self.generated_at,
            "sample_size": self.sample_size,
            "source_hash": self.source_hash,
            "freshness_ttl": self.spec.freshness_ttl_hours,
            "freshness_ttl_hours": self.spec.freshness_ttl_hours,
            "owner_module": self.spec.owner_module,
            "status": self.status,
            "accepted": self.accepted,
            "modified_at": self.modified_at,
            "age_hours": self.age_hours,
            "reasons": list(self.reasons),
        }
        if include_payload:
            result["payload"] = self.payload
        return result


@dataclass(frozen=True)
class Evidence:
    """One normalized research observation."""

    evidence_id: str
    source: str
    category: str
    hypothesis: str
    metric: str
    value: Any
    sample_size: int
    confidence: str
    freshness_status: str
    quality_status: str
    direction: str
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Conflict:
    """Contradictory evidence associated with one hypothesis."""

    conflict_id: str
    hypothesis: str
    supporting_sources: tuple[str, ...]
    contradicting_sources: tuple[str, ...]
    severity: str
    resolution: str
    action: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["supporting_sources"] = list(self.supporting_sources)
        result["contradicting_sources"] = list(self.contradicting_sources)
        return result


@dataclass
class HypothesisAssessment:
    """Evidence-gated status for one research hypothesis."""

    hypothesis: str
    status: str
    confidence: str
    metrics: dict[str, Any]
    supporting_sources: list[str]
    contradicting_sources: list[str]
    evidence_count: int
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
