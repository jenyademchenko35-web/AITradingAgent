"""Registry and safe JSON loader for Research Orchestrator artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from research_orchestrator.freshness_gate import FreshnessGate
from research_orchestrator.models import ArtifactRecord, ArtifactSpec


DEFAULT_ARTIFACTS = (
    ArtifactSpec("trade_metrics_audit", "METRICS", "trade_metrics_audit.json", True, "1.0", ("R",), 24, "trade_metrics_normalizer.py", True),
    ArtifactSpec("hypothesis_report", "HYPOTHESIS", "hypothesis_report.json", False, "1.0", ("R",), 24, "strategy_lab/hypothesis_runner.py", True),
    ArtifactSpec("strategy_lab_report", "HYPOTHESIS", "strategy_lab_report.json", False, "1.0", ("R",), 24, "strategy_lab/runner.py", True),
    ArtifactSpec("trade_loss_report", "TRADE_ANALYSIS", "trade_loss_report.json", False, "1.0", ("R",), 24, "trade_loss_analyzer.py", True),
    ArtifactSpec("trade_memory_report", "MEMORY", "trade_memory_report.json", False, "1.0", ("R",), 6, "trade_memory.py", True),
    ArtifactSpec("trade_replay_report", "REPLAY", "trade_replay_report.json", False, "1.0", ("R",), 72, "trade_replay_lab", True),
    ArtifactSpec("shadow_replay_report", "REPLAY", "shadow_replay_report.json", False, "2.0", ("R",), 72, "shadow_replay.py", True),
    ArtifactSpec("market_intelligence_report", "CONTEXT", "market_intelligence_report.json", False, "1.0", ("CONTEXT",), 2, "market_intelligence_hub.py"),
    ArtifactSpec("post_trade_intelligence", "TRADE_ANALYSIS", "post_trade_intelligence.json", False, "1.0", ("R",), 24, "post_trade_intelligence.py", True),
    ArtifactSpec("news_statistics_report", "NEWS", "news_statistics_report.json", False, "1.0", ("R", "COUNT"), 24, "news_statistics.py", True),
    ArtifactSpec("research_consensus_report", "CONSENSUS", "research_consensus_report.json", False, "1.0", ("R",), 24, "research_consensus/consensus_engine.py", True),
    ArtifactSpec("runtime_data_foundation_report", "DATA_QUALITY", "runtime_data_foundation_report.json", False, "1.0", ("BYTES",), 168, "runtime_data_foundation.py", True),
    ArtifactSpec("research_integrity_report", "DATA_QUALITY", "research_integrity_report.json", False, "1.0", ("CONTEXT", "COUNT"), 168, "research_integrity.py"),
    ArtifactSpec("live_monitor_state", "EXECUTION", "live_monitor_state.json", False, "1.0", ("CONTEXT",), 30 / 3600, "live_market_monitor.py"),
    ArtifactSpec("market_news_feed", "NEWS", "market_news_feed.json", False, "1.0", ("SENTIMENT", "CONTEXT"), 2, "market_news_observer.py"),
    ArtifactSpec("agent_v3_stats", "EXECUTION", "agent_v3_stats.json", False, "1.0", ("COUNT",), 24, "multi_timeframe_agent_v3.py"),
)


class ArtifactRegistry:
    """Load report artifacts and expose only validated payloads as evidence."""

    def __init__(
        self,
        base_dir: Path | str,
        *,
        specs: Iterable[ArtifactSpec] = DEFAULT_ARTIFACTS,
        now: datetime | None = None,
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.specs = tuple(specs)
        self.now = now or datetime.now(timezone.utc)
        if self.now.tzinfo is None:
            self.now = self.now.replace(tzinfo=timezone.utc)
        self.records: dict[str, ArtifactRecord] = {}

    def load_all(self) -> dict[str, ArtifactRecord]:
        """Load every registered artifact without invoking its owner module."""
        self.records.clear()
        canonical = self._canonical_context()
        gate = FreshnessGate(
            self.base_dir,
            now=self.now,
            canonical_closed=canonical["closed"],
            canonical_complete=canonical["complete"],
            normalizer_version=canonical["normalizer_version"],
        )
        for spec in self.specs:
            self.records[spec.artifact_name] = self._load_one(spec, gate)
        return self.records

    def accepted_payloads(self) -> dict[str, dict[str, Any]]:
        """Return only VALID report payloads."""
        return {
            name: record.payload
            for name, record in self.records.items()
            if record.accepted
        }

    def diagnostics(self) -> dict[str, Any]:
        """Group artifacts by quality status for report diagnostics."""
        result: dict[str, Any] = {
            "accepted_reports": [],
            "missing_artifacts": [],
            "stale_artifacts": [],
            "incompatible_artifacts": [],
            "corrupted_artifacts": [],
            "legacy_artifacts": [],
            "empty_artifacts": [],
            "incompatible_metric_units": [],
            "different_sample_sizes": [],
            "source_hash_mismatch": [],
            "normalizer_mismatch": [],
        }
        destinations = {
            "VALID": "accepted_reports",
            "MISSING": "missing_artifacts",
            "STALE": "stale_artifacts",
            "INCOMPATIBLE": "incompatible_artifacts",
            "CORRUPTED": "corrupted_artifacts",
            "LEGACY": "legacy_artifacts",
            "EMPTY": "empty_artifacts",
        }
        for record in self.records.values():
            item = record.to_dict()
            result[destinations[record.status]].append(item)
            for reason in record.reasons:
                if "metric_unit=" in reason:
                    result["incompatible_metric_units"].append(item)
                if "sample size mismatch" in reason:
                    result["different_sample_sizes"].append(item)
                if "source hash" in reason or "source fingerprint" in reason:
                    result["source_hash_mismatch"].append(item)
                if "normalizer mismatch" in reason:
                    result["normalizer_mismatch"].append(item)
        return result

    def _canonical_context(self) -> dict[str, Any]:
        spec = next(
            (item for item in self.specs if item.artifact_name == "trade_metrics_audit"),
            None,
        )
        if spec is None:
            return {"closed": 0, "complete": 0, "normalizer_version": ""}
        payload, _, _ = self._read_json(self.base_dir / spec.path)
        metadata = payload.get("metadata", {}) if payload else {}
        metrics = payload.get("metrics", {}) if payload else {}
        return {
            "closed": self._safe_int(
                metadata.get("closed_trades_total") or metrics.get("closed_trades")
            ),
            "complete": self._safe_int(
                metadata.get("complete_metrics_total") or metrics.get("metrics_trades")
            ),
            "normalizer_version": str(
                metadata.get("normalizer_version")
                or metadata.get("generator_version")
                or ""
            ),
        }

    def _load_one(
        self,
        spec: ArtifactSpec,
        gate: FreshnessGate,
    ) -> ArtifactRecord:
        path = self.base_dir / spec.path
        payload, status, reasons = self._read_json(path)
        metadata = payload.get("metadata", {}) if payload else {}
        result = None
        if status == "VALID" and (
            "metadata" not in payload
            or not isinstance(metadata, Mapping)
            or not metadata
        ):
            status = "LEGACY"
            reasons = ["metadata отсутствует"]
            metadata = {}
        elif status == "VALID":
            result = gate.validate(spec, payload, metadata)
            status = result.status
            reasons = list(result.reasons)

        generated_at = str(
            metadata.get("generated_at")
            or payload.get("generated_at")
            or ""
        )
        return ArtifactRecord(
            spec=spec,
            resolved_path=path,
            status=status,
            reasons=reasons,
            payload=payload,
            metadata=dict(metadata),
            generated_at=generated_at,
            modified_at=self._modified_at(path),
            sample_size=self._sample_size(payload, metadata),
            source_hash=(
                metadata.get("source_file_hashes")
                or metadata.get("source_file_fingerprints")
                or ""
            ),
            age_hours=result.age_hours if result else None,
        )

    @staticmethod
    def _read_json(path: Path) -> tuple[dict[str, Any], str, list[str]]:
        if not path.exists():
            return {}, "MISSING", ["отчёт отсутствует"]
        try:
            if path.stat().st_size == 0:
                return {}, "EMPTY", ["отчёт пуст"]
            with path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            return {}, "CORRUPTED", [f"ошибка чтения: {type(exc).__name__}"]
        if not isinstance(payload, dict):
            return {}, "CORRUPTED", ["корневое значение JSON не является объектом"]
        if not payload:
            return {}, "EMPTY", ["JSON-объект пуст"]
        return payload, "VALID", []

    @staticmethod
    def _sample_size(
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> int:
        candidates = [
            metadata.get("complete_metrics_total"),
            payload.get("sample_size"),
            payload.get("opportunities"),
            payload.get("closed_trades"),
        ]
        for block_name in ("metrics", "stats", "sample"):
            block = payload.get(block_name, {})
            if isinstance(block, Mapping):
                candidates.extend([
                    block.get("metrics_trades"),
                    block.get("trades"),
                    block.get("closed_trades"),
                ])
        for value in candidates:
            parsed = ArtifactRegistry._safe_int(value)
            if parsed:
                return parsed
        return 0

    @staticmethod
    def _modified_at(path: Path) -> str:
        if not path.exists():
            return ""
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0
