"""Top-level read-only Research Orchestrator service."""

from __future__ import annotations

import csv
import json
import logging
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from report_metadata import build_report_metadata
from research_orchestrator.artifact_registry import ArtifactRegistry
from research_orchestrator.conflict_resolver import ConflictResolver
from research_orchestrator.evidence_gate import EvidenceBuilder, EvidenceGate, safe_int
from research_orchestrator.formatter import format_summary
from research_orchestrator.models import Conflict, Evidence, HypothesisAssessment
from research_orchestrator.recommendation_engine import RecommendationEngine


BASE_DIR = Path(__file__).resolve().parents[1]
REPORT_FILE = BASE_DIR / "research_orchestrator_report.json"
SUMMARY_FILE = BASE_DIR / "research_orchestrator_summary.txt"
EVIDENCE_FILE = BASE_DIR / "research_orchestrator_evidence.csv"
CONFLICTS_FILE = BASE_DIR / "research_orchestrator_conflicts.csv"
LOG_FILE = BASE_DIR / "research_orchestrator.log"

SAFE_REFRESH_MODULES = (
    "trade_metrics_normalizer.py",
    "strategy_lab/runner.py",
    "strategy_lab/hypothesis_runner.py",
    "trade_loss_analyzer.py",
    "trade_memory.py",
    "market_intelligence_hub.py",
)


class ResearchOrchestrator:
    """Coordinate existing reports without changing any trading component."""

    def __init__(
        self,
        base_dir: Path | str = BASE_DIR,
        *,
        now: datetime | None = None,
        registry: ArtifactRegistry | None = None,
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.now = now or datetime.now(timezone.utc)
        if self.now.tzinfo is None:
            self.now = self.now.replace(tzinfo=timezone.utc)
        self.registry = registry or ArtifactRegistry(self.base_dir, now=self.now)
        self.logger = self._logger()

    def build_report(self, *, save: bool = True) -> dict[str, Any]:
        """Build one research snapshot strictly from existing report files."""
        records = self.registry.load_all()
        diagnostics = self.registry.diagnostics()
        payloads = self.registry.accepted_payloads()
        for record in records.values():
            message = "принят" if record.accepted else "исключён"
            self.logger.info(
                "artifact=%s status=%s result=%s reasons=%s",
                record.spec.artifact_name,
                record.status,
                message,
                "; ".join(record.reasons) or "none",
            )

        evidence, candidates, baseline = EvidenceBuilder().build(payloads)
        critical_quality = self._critical_data_quality(payloads, records)
        assessments = EvidenceGate().assess_all(
            candidates,
            baseline,
            evidence,
            critical_data_quality=critical_quality,
        )
        conflicts = ConflictResolver().resolve(evidence)
        assessments = self._apply_conflicts(assessments, conflicts)
        diagnostics.update(self._data_quality_details(payloads, conflicts))
        recommendation = RecommendationEngine().build(
            assessments,
            conflicts,
            diagnostics,
            critical_data_quality=critical_quality,
        )
        leader = RecommendationEngine().select_leader(assessments)
        status = self._overall_status(records, conflicts, critical_quality)

        source_files = [
            record.resolved_path
            for record in records.values()
            if record.accepted
        ]
        generated_at = self.now.astimezone(timezone.utc).isoformat()
        canonical_record = records.get("trade_metrics_audit")
        canonical_metadata = (
            canonical_record.metadata if canonical_record else {}
        )
        report_metadata = build_report_metadata(
            generator="research_orchestrator.ResearchOrchestrator",
            generator_version="1.0",
            metric_unit="R",
            source_files=source_files,
            base_dir=self.base_dir,
            data_period_start=str(
                canonical_metadata.get("data_period_start", "")
            ),
            data_period_end=str(
                canonical_metadata.get("data_period_end", "")
            ),
            closed_trades_total=safe_int(baseline.get("closed_trades")),
            complete_metrics_total=safe_int(baseline.get("metrics_trades")),
            generated_at=generated_at,
        )
        report_metadata["normalizer_version"] = str(
            canonical_metadata.get("normalizer_version")
            or canonical_metadata.get("generator_version")
            or ""
        )
        report_metadata["freshness_ttl_hours"] = 24
        report = {
            "generated_at": generated_at,
            "metadata": report_metadata,
            "status": status,
            "mode": "READ_ONLY_RESEARCH",
            "canonical_metrics": baseline,
            "artifacts": [record.to_dict() for record in records.values()],
            "diagnostics": diagnostics,
            "evidence": [item.to_dict() for item in evidence],
            "hypotheses": [item.to_dict() for item in assessments],
            "conflicts": [item.to_dict() for item in conflicts],
            "main_candidate": (
                leader.to_dict()
                if leader
                else {
                    "hypothesis": "Нет подтверждённого кандидата",
                    "status": "INSUFFICIENT_DATA",
                    "confidence": "VERY_LOW",
                    "supporting_sources": [],
                    "contradicting_sources": [],
                }
            ),
            "recommendation": recommendation,
            "restrictions": {
                "read_only": True,
                "automatic_live_change": False,
                "decision_engine_unchanged": True,
                "trade_execution_disabled": True,
            },
        }
        self.logger.info(
            "leader=%s conflicts=%s recommendation=%s",
            recommendation.get("leader"),
            len(conflicts),
            recommendation.get("primary"),
        )
        if save:
            self.save_report(report)
        return report

    def refresh_safe_reports(self, timeout_seconds: int = 180) -> list[dict[str, Any]]:
        """Run the explicit allowlist of read-only report generators."""
        results = []
        for relative in SAFE_REFRESH_MODULES:
            script = self.base_dir / relative
            if not script.exists():
                results.append({"module": relative, "status": "MISSING"})
                continue
            try:
                completed = subprocess.run(
                    [sys.executable, str(script)],
                    cwd=self.base_dir,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                results.append({
                    "module": relative,
                    "status": "ERROR",
                    "error": type(exc).__name__,
                })
                continue
            results.append({
                "module": relative,
                "status": "OK" if completed.returncode == 0 else "ERROR",
                "return_code": completed.returncode,
            })
        return results

    def save_report(self, report: Mapping[str, Any]) -> None:
        """Atomically persist JSON and regenerate compact CSV/TXT views."""
        self._write_json_atomic(self.base_dir / REPORT_FILE.name, report)
        (self.base_dir / SUMMARY_FILE.name).write_text(
            format_summary(report) + "\n",
            encoding="utf-8",
        )
        self._write_csv(
            self.base_dir / EVIDENCE_FILE.name,
            report.get("evidence", []),
            (
                "evidence_id", "source", "category", "hypothesis", "metric",
                "value", "sample_size", "confidence", "freshness_status",
                "quality_status", "direction", "notes",
            ),
        )
        self._write_csv(
            self.base_dir / CONFLICTS_FILE.name,
            report.get("conflicts", []),
            (
                "conflict_id", "hypothesis", "supporting_sources",
                "contradicting_sources", "severity", "resolution", "action",
            ),
        )

    def _logger(self) -> logging.Logger:
        name = f"research_orchestrator:{self.base_dir}"
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            handler = logging.FileHandler(
                self.base_dir / LOG_FILE.name,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s"
            ))
            logger.addHandler(handler)
        return logger

    @staticmethod
    def _apply_conflicts(
        assessments: Iterable[HypothesisAssessment],
        conflicts: Iterable[Conflict],
    ) -> list[HypothesisAssessment]:
        conflict_names = {item.hypothesis for item in conflicts}
        result = []
        for assessment in assessments:
            if assessment.hypothesis in conflict_names:
                result.append(replace(
                    assessment,
                    status="CONFLICTED",
                    reasons=assessment.reasons + [
                        "Conflict Resolver обнаружил поддержку и опровержение."
                    ],
                ))
            else:
                result.append(assessment)
        return result

    @staticmethod
    def _critical_data_quality(
        payloads: Mapping[str, Mapping[str, Any]],
        records: Mapping[str, Any],
    ) -> bool:
        integrity = payloads.get("research_integrity_report", {})
        severity = str(
            integrity.get("severity")
            or integrity.get("status")
            or ""
        ).upper()
        if "CRITICAL" in severity:
            return True
        audit = payloads.get("trade_metrics_audit", {})
        warnings = audit.get("warnings", [])
        if any("CRITICAL" in str(item).upper() for item in warnings):
            return True
        required_failures = [
            record for record in records.values()
            if record.spec.required
            and record.status in {"CORRUPTED", "INCOMPATIBLE", "MISSING", "EMPTY"}
        ]
        return bool(required_failures)

    @staticmethod
    def _data_quality_details(
        payloads: Mapping[str, Mapping[str, Any]],
        conflicts: Iterable[Conflict],
    ) -> dict[str, Any]:
        audit = payloads.get("trade_metrics_audit", {})
        metrics = audit.get("metrics", {}) if isinstance(
            audit.get("metrics"), Mapping
        ) else {}
        memory = payloads.get("trade_memory_report", {})
        stats = memory.get("stats", {}) if isinstance(
            memory.get("stats"), Mapping
        ) else {}
        qualities = stats.get("subset_match_quality", {})
        legacy_matches = sum(
            safe_int(count)
            for name, count in qualities.items()
            if "LEGACY" in str(name).upper()
        ) if isinstance(qualities, Mapping) else 0
        matched = safe_int(stats.get("matched_similar_total"))
        return {
            "legacy_linkage_share": (
                round(legacy_matches / matched * 100, 2) if matched else 0.0
            ),
            "incomplete_metrics": safe_int(metrics.get("incomplete_metrics")),
            "unresolved_conflicts": len(list(conflicts)),
        }

    @staticmethod
    def _overall_status(
        records: Mapping[str, Any],
        conflicts: Iterable[Conflict],
        critical_quality: bool,
    ) -> str:
        if critical_quality:
            return "DEGRADED"
        if any(not record.accepted for record in records.values()) or list(conflicts):
            return "WARNING"
        return "OK"

    @staticmethod
    def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _write_csv(
        path: Path,
        rows: Any,
        fieldnames: tuple[str, ...],
    ) -> None:
        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for raw_row in rows if isinstance(rows, list) else []:
                row = dict(raw_row)
                for key, value in row.items():
                    if isinstance(value, (dict, list, tuple)):
                        row[key] = json.dumps(value, ensure_ascii=False)
                writer.writerow(row)


def main() -> int:
    """Run one read-only orchestration pass."""
    report = ResearchOrchestrator().build_report(save=True)
    print(format_summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
