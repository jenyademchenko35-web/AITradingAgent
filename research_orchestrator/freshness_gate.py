"""Freshness, provenance and compatibility validation for research reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from report_metadata import (
    fingerprints_match,
    metadata_age_hours,
    normalize_metric_unit,
    resolve_source_path,
    source_file_fingerprint,
    source_file_sha256,
)
from research_orchestrator.models import ArtifactSpec


@dataclass(frozen=True)
class GateResult:
    """Validation result returned to Artifact Registry."""

    status: str
    reasons: tuple[str, ...]
    age_hours: float | None


class FreshnessGate:
    """Reject stale or incompatible research without running generators."""

    def __init__(
        self,
        base_dir: Path,
        *,
        now: datetime | None = None,
        canonical_closed: int = 0,
        canonical_complete: int = 0,
        normalizer_version: str = "",
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.now = now or datetime.now(timezone.utc)
        if self.now.tzinfo is None:
            self.now = self.now.replace(tzinfo=timezone.utc)
        self.canonical_closed = max(int(canonical_closed or 0), 0)
        self.canonical_complete = max(int(canonical_complete or 0), 0)
        self.normalizer_version = str(normalizer_version or "")

    def validate(
        self,
        spec: ArtifactSpec,
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> GateResult:
        """Validate schema, unit, TTL, lineage and canonical sample."""
        incompatible: list[str] = []
        stale: list[str] = []

        schema = str(metadata.get("schema_version") or "")
        if schema != spec.schema_version:
            incompatible.append(
                f"schema_version={schema or 'не указан'}, ожидается {spec.schema_version}"
            )

        unit = normalize_metric_unit(metadata.get("metric_unit"))
        expected_units = {
            normalize_metric_unit(item) for item in spec.metric_units
        }
        if unit not in expected_units:
            incompatible.append(
                f"metric_unit={unit}, ожидается {sorted(expected_units)}"
            )

        report_normalizer = str(metadata.get("normalizer_version") or "")
        if (
            report_normalizer
            and self.normalizer_version
            and report_normalizer != self.normalizer_version
        ):
            incompatible.append(
                "normalizer mismatch: "
                f"{report_normalizer} != {self.normalizer_version}"
            )

        age = metadata_age_hours(metadata, now=self.now)
        ttl = self._metadata_ttl_hours(metadata, spec.freshness_ttl_hours)
        if age is None:
            stale.append("generated_at отсутствует или некорректен")
        elif age < -(5 / 60):
            stale.append(f"generated_at находится в будущем на {abs(age):.2f} ч")
        elif age > ttl:
            stale.append(f"TTL превышен: {age:.2f} ч > {ttl:g} ч")

        stale.extend(self._lineage_reasons(metadata))
        if spec.sample_bound:
            stale.extend(self._sample_reasons(metadata))

        if incompatible:
            return GateResult("INCOMPATIBLE", tuple(incompatible), age)
        if stale:
            return GateResult("STALE", tuple(stale), age)
        return GateResult("VALID", (), age)

    @staticmethod
    def _metadata_ttl_hours(
        metadata: Mapping[str, Any],
        default: float,
    ) -> float:
        for key in ("freshness_ttl_hours", "ttl_hours"):
            try:
                value = float(metadata.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        try:
            seconds = float(metadata.get("freshness_ttl_seconds"))
        except (TypeError, ValueError):
            seconds = 0.0
        return seconds / 3600 if seconds > 0 else float(default)

    def _lineage_reasons(self, metadata: Mapping[str, Any]) -> list[str]:
        sources = metadata.get("source_files")
        fingerprints = metadata.get("source_file_fingerprints")
        hashes = metadata.get("source_file_hashes")
        if not isinstance(sources, list) or not sources:
            return ["source lineage отсутствует"]
        if not isinstance(fingerprints, Mapping) and not isinstance(hashes, Mapping):
            return ["source hash/fingerprint отсутствует"]

        reasons: list[str] = []
        for raw_name in sources:
            name = str(raw_name)
            path = resolve_source_path(name, self.base_dir)
            expected = (
                fingerprints.get(name)
                if isinstance(fingerprints, Mapping)
                else None
            )
            if isinstance(expected, Mapping):
                if not fingerprints_match(expected, source_file_fingerprint(path)):
                    reasons.append(f"source fingerprint mismatch: {name}")
                continue
            expected_hash = hashes.get(name) if isinstance(hashes, Mapping) else ""
            if not expected_hash:
                reasons.append(f"source hash отсутствует: {name}")
            elif source_file_sha256(path) != expected_hash:
                reasons.append(f"source hash mismatch: {name}")
        return reasons

    def _sample_reasons(self, metadata: Mapping[str, Any]) -> list[str]:
        reasons = []
        closed = self._safe_int(metadata.get("closed_trades_total"))
        complete = self._safe_int(metadata.get("complete_metrics_total"))
        if self.canonical_closed and closed != self.canonical_closed:
            reasons.append(
                f"sample size mismatch: closed {closed} != {self.canonical_closed}"
            )
        if self.canonical_complete and complete != self.canonical_complete:
            reasons.append(
                "sample size mismatch: complete "
                f"{complete} != {self.canonical_complete}"
            )
        return reasons

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0
