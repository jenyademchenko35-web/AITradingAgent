"""Compatibility and freshness gate for Adaptive Research artifacts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from report_metadata import normalize_metric_unit
from research_orchestrator.freshness_gate import FreshnessGate
from research_orchestrator.models import ArtifactSpec as GateArtifactSpec

from adaptive_research.state import TradeFingerprint, sha256_file


@dataclass(frozen=True)
class ArtifactSpec:
    """Expected contract for one research report."""

    key: str
    path: str
    schema_version: str
    metric_units: tuple[str, ...] = ("R",)
    ttl_hours: float = 24.0
    sample_bound: bool = True
    owner: str = ""


ARTIFACT_SPECS = (
    ArtifactSpec("metrics", "trade_metrics_audit.json", "1.0", ("R",), 24, True, "trade_metrics_normalizer.py"),
    ArtifactSpec("loss", "trade_loss_report.json", "1.0", ("R",), 24, True, "trade_loss_analyzer.py"),
    ArtifactSpec("memory", "trade_memory_report.json", "1.0", ("R",), 24, True, "trade_memory.py"),
    ArtifactSpec("replay", "shadow_replay_report.json", "2.0", ("R",), 72, True, "shadow_replay.py"),
    ArtifactSpec("lab", "hypothesis_report.json", "1.0", ("R",), 24, True, "strategy_lab/hypothesis_runner.py"),
    ArtifactSpec("orchestrator", "research_orchestrator_report.json", "1.0", ("R",), 24, False, "research_orchestrator.py"),
    ArtifactSpec("consensus", "research_consensus_report.json", "1.0", ("R",), 24, True, "research_consensus/consensus_engine.py"),
    ArtifactSpec("news", "market_news_feed.json", "1.0", ("SENTIMENT", "CONTEXT"), 2, False, "market_news_observer.py"),
)

SPEC_BY_KEY = {spec.key: spec for spec in ARTIFACT_SPECS}


@dataclass
class ArtifactRecord:
    """Validated report and its immutable pipeline lineage."""

    key: str
    path: str
    status: str
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    schema: str = ""
    freshness: str = ""
    source: str = "trades.csv"
    version: str = ""
    hash: str = ""
    created_at: str = ""
    metric_unit: str = ""
    source_trade_hash: str = ""
    trade_count: int = 0
    age_hours: float | None = None
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self, include_payload: bool = False) -> dict[str, Any]:
        result = asdict(self)
        if not include_payload:
            result.pop("payload", None)
        return result


class ArtifactRegistry:
    """Accept only current reports matching schema, unit, source and hash."""

    def __init__(
        self,
        base_dir: Path,
        fingerprint: TradeFingerprint,
        previous_records: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.fingerprint = fingerprint
        self.previous_records = dict(previous_records or {})
        self.now = now or datetime.now(timezone.utc)
        if self.now.tzinfo is None:
            self.now = self.now.replace(tzinfo=timezone.utc)
        self.records: dict[str, ArtifactRecord] = {}
        self._canonical = self._canonical_context()

    def inspect_all(
        self,
        generated_keys: set[str] | None = None,
    ) -> dict[str, ArtifactRecord]:
        """Inspect all known artifacts and remember accepted payloads."""
        generated = generated_keys or set()
        self.records = {
            spec.key: self.inspect(spec, newly_generated=spec.key in generated)
            for spec in ARTIFACT_SPECS
        }
        return self.records

    def inspect(
        self,
        spec: ArtifactSpec,
        *,
        newly_generated: bool = False,
    ) -> ArtifactRecord:
        path = self.base_dir / spec.path
        payload, read_error = self._read_payload(path)
        if read_error:
            return self._record(spec, "IGNORED", [read_error], payload={})
        metadata = payload.get("metadata")
        if not isinstance(metadata, Mapping) or not metadata:
            return self._record(
                spec,
                "IGNORED",
                ["metadata отсутствует"],
                payload=payload,
            )

        report_hash = sha256_file(path)
        reasons: list[str] = []
        status = "CURRENT"
        previous = self.previous_records.get(spec.key, {})
        created_at = str(
            metadata.get("generated_at") or payload.get("generated_at") or ""
        )
        if (
            not newly_generated
            and isinstance(previous, Mapping)
            and previous
            and str(previous.get("hash") or "")
            and str(previous.get("hash")) != report_hash
            and str(previous.get("created_at") or "") == created_at
        ):
            reasons.append("artifact hash mismatch без нового created_at")
            status = "IGNORED"

        gate_spec = GateArtifactSpec(
            artifact_name=spec.key,
            report_type="ADAPTIVE_RESEARCH",
            path=spec.path,
            required=False,
            schema_version=spec.schema_version,
            metric_units=spec.metric_units,
            freshness_ttl_hours=spec.ttl_hours,
            owner_module=spec.owner,
            sample_bound=spec.sample_bound,
        )
        gate = FreshnessGate(
            self.base_dir,
            now=self.now,
            canonical_closed=self._canonical["closed"],
            canonical_complete=self._canonical["complete"],
            normalizer_version=self._canonical["normalizer_version"],
        )
        gate_result = gate.validate(gate_spec, payload, metadata)
        reasons.extend(gate_result.reasons)
        if gate_result.status == "STALE" and status == "CURRENT":
            status = "STALE"
        elif gate_result.status != "VALID":
            status = "IGNORED"
        if spec.key == "news" and status == "CURRENT":
            news_reasons = self._news_reasons(payload, metadata)
            if news_reasons:
                reasons.extend(news_reasons)
                status = "STALE"

        record = ArtifactRecord(
            key=spec.key,
            path=spec.path,
            status=status,
            accepted=status == "CURRENT",
            reasons=list(dict.fromkeys(reasons)),
            schema=str(metadata.get("schema_version") or ""),
            freshness=status,
            source=(
                "market_news_feed.json"
                if spec.key == "news"
                else "trades.csv"
            ),
            version=str(metadata.get("generator_version") or ""),
            hash=report_hash,
            created_at=created_at,
            metric_unit=normalize_metric_unit(metadata.get("metric_unit")),
            source_trade_hash=(
                "" if spec.key == "news" else self.fingerprint.trade_hash
            ),
            trade_count=(
                0 if spec.key == "news" else self.fingerprint.trade_count
            ),
            age_hours=gate_result.age_hours,
            payload=payload,
        )
        return record

    def _news_reasons(
        self,
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> list[str]:
        """Reject stale Shadow News without ever running its fetcher."""
        reasons: list[str] = []
        observer_status = str(
            payload.get("status") or metadata.get("status") or ""
        ).strip().upper()
        if observer_status not in {"OK", "PARTIAL"}:
            reasons.append(
                f"news observer status={observer_status or 'UNKNOWN'}"
            )
        summary = payload.get("summary", {})
        recent = summary.get("recent_24h", 0) if isinstance(summary, Mapping) else 0
        if self._safe_int(metadata.get("news_24h") or recent) <= 0:
            reasons.append("news_24h=0")
        last_success = self._parse_timestamp(
            metadata.get("last_success_at")
            or payload.get("last_success_at")
        )
        if last_success is None:
            reasons.append("last_success_at отсутствует")
        elif (self.now - last_success).total_seconds() > 2 * 3600:
            reasons.append("news feed старше 2 часов")
        return reasons

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def accepted_payloads(self) -> dict[str, dict[str, Any]]:
        """Return payloads that passed every research gate."""
        return {
            key: record.payload
            for key, record in self.records.items()
            if record.accepted
        }

    def diagnostics(self) -> dict[str, Any]:
        """Return compact artifact gate counters and exclusion reasons."""
        accepted = [record.key for record in self.records.values() if record.accepted]
        stale = [record.to_dict() for record in self.records.values() if record.status == "STALE"]
        ignored = [record.to_dict() for record in self.records.values() if record.status == "IGNORED"]
        return {
            "accepted": accepted,
            "accepted_total": len(accepted),
            "stale": stale,
            "stale_total": len(stale),
            "ignored": ignored,
            "ignored_total": len(ignored),
        }

    def _canonical_context(self) -> dict[str, Any]:
        payload, error = self._read_payload(self.base_dir / "trade_metrics_audit.json")
        if error:
            return {
                "closed": self.fingerprint.trade_count,
                "complete": 0,
                "normalizer_version": "",
            }
        metadata = payload.get("metadata", {})
        metrics = payload.get("metrics", {})
        closed = self._safe_int(
            metadata.get("closed_trades_total") or metrics.get("closed_trades")
        )
        if closed != self.fingerprint.trade_count:
            return {
                "closed": self.fingerprint.trade_count,
                "complete": 0,
                "normalizer_version": "",
            }
        return {
            "closed": closed,
            "complete": self._safe_int(
                metadata.get("complete_metrics_total") or metrics.get("metrics_trades")
            ),
            "normalizer_version": str(
                metadata.get("normalizer_version")
                or metadata.get("generator_version")
                or ""
            ),
        }

    @staticmethod
    def _read_payload(path: Path) -> tuple[dict[str, Any], str]:
        if not path.exists():
            return {}, "отчёт отсутствует"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {}, f"ошибка чтения: {type(exc).__name__}"
        if not isinstance(payload, dict) or not payload:
            return {}, "отчёт пуст или имеет неверный формат"
        return payload, ""

    def _record(
        self,
        spec: ArtifactSpec,
        status: str,
        reasons: list[str],
        payload: dict[str, Any],
    ) -> ArtifactRecord:
        path = self.base_dir / spec.path
        return ArtifactRecord(
            key=spec.key,
            path=spec.path,
            status=status,
            accepted=False,
            reasons=reasons,
            freshness=status,
            source=(
                "market_news_feed.json"
                if spec.key == "news"
                else "trades.csv"
            ),
            hash=sha256_file(path),
            source_trade_hash=(
                "" if spec.key == "news" else self.fingerprint.trade_hash
            ),
            trade_count=(
                0 if spec.key == "news" else self.fingerprint.trade_count
            ),
            payload=payload,
        )

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0
