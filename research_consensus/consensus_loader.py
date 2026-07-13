"""Freshness-aware loader for existing AITradingAgent research reports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from report_metadata import (
    SUPPORTED_SCHEMA_VERSIONS,
    fingerprints_match,
    metadata_age_hours,
    normalize_metric_unit,
    resolve_source_path,
    source_file_fingerprint,
    source_file_sha256,
)
from trade_metrics_normalizer import aggregate_trade_metrics, read_trade_rows


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

TRADE_METRIC_REPORTS = frozenset({
    "trade_loss",
    "trade_replay",
    "strategy_lab",
    "hypothesis_lab",
    "trade_memory",
    "news_statistics",
    "market_regime",
})
CONTEXT_REPORTS = frozenset(REPORT_FILES) - TRADE_METRIC_REPORTS


class ConsensusLoader:
    """Load only current, compatible reports without invoking generators."""

    def __init__(
        self,
        base_dir: Path = BASE_DIR,
        *,
        report_files: Mapping[str, str] | None = None,
        ttl_hours: float = 24.0,
        now: datetime | None = None,
        trades_file: Path | None = None,
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.report_files = dict(report_files or REPORT_FILES)
        self.ttl_hours = max(float(ttl_hours), 0.0)
        self.now = now or datetime.now(timezone.utc)
        if self.now.tzinfo is None:
            self.now = self.now.replace(tzinfo=timezone.utc)
        self.trades_file = trades_file or self.base_dir / "trades.csv"
        self.canonical_metrics = aggregate_trade_metrics(
            read_trade_rows(self.trades_file)
        )
        self.reports: dict[str, dict[str, Any]] = {}
        self.raw_reports: dict[str, dict[str, Any]] = {}
        self.source_status: dict[str, dict[str, Any]] = {}
        self.warnings: list[str] = []
        self.load_all()

    def load_all(self) -> dict[str, dict[str, Any]]:
        """Read reports and expose only CURRENT reports for consensus voting."""
        self.reports.clear()
        self.raw_reports.clear()
        self.source_status.clear()
        self.warnings.clear()

        for key, filename in self.report_files.items():
            path = self.base_dir / filename
            report, error = self.read_report(path)
            if report:
                self.raw_reports[key] = report

            if error:
                state = "MISSING" if not path.exists() else "INCOMPATIBLE"
                reasons = [error]
            else:
                state, reasons = self._validate_report(key, report)

            accepted = state == "CURRENT"
            if accepted:
                self.reports[key] = report
            if state == "INCOMPATIBLE" and path.exists():
                self.warnings.append(f"{filename}: {'; '.join(reasons)}")

            metadata = report.get("metadata", {}) if report else {}
            self.source_status[key] = {
                "file": filename,
                "exists": path.exists(),
                "valid": accepted,
                "accepted": accepted,
                "state": state,
                "generated_at": metadata.get("generated_at", ""),
                "modified_at": self.modified_at(path),
                "size_bytes": path.stat().st_size if path.exists() else 0,
                "metric_unit": metadata.get("metric_unit", ""),
                "reasons": reasons,
                "error": error,
            }
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

    def _validate_report(
        self,
        key: str,
        report: Mapping[str, Any],
    ) -> tuple[str, list[str]]:
        metadata = report.get("metadata")
        if not isinstance(metadata, Mapping):
            return "STALE", ["metadata отсутствует"]

        schema_version = str(metadata.get("schema_version") or "")
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            return "INCOMPATIBLE", [
                f"schema_version {schema_version or 'не указан'} не поддерживается"
            ]

        metric_unit = normalize_metric_unit(metadata.get("metric_unit"))
        expected_units = self._expected_metric_units(key)
        if metric_unit not in expected_units:
            return "INCOMPATIBLE", [
                f"metric_unit={metric_unit}, ожидается {sorted(expected_units)}"
            ]

        stale_reasons: list[str] = []
        required_fields = (
            "generated_at",
            "source_files",
            "data_period_start",
            "data_period_end",
            "closed_trades_total",
            "complete_metrics_total",
            "generator",
            "generator_version",
        )
        missing_fields = [field for field in required_fields if field not in metadata]
        if missing_fields:
            stale_reasons.append(
                "metadata fields отсутствуют: " + ", ".join(missing_fields)
            )
        age_hours = metadata_age_hours(metadata, now=self.now)
        if age_hours is None:
            stale_reasons.append("generated_at отсутствует или некорректен")
        elif age_hours < -(5 / 60):
            stale_reasons.append(
                f"generated_at находится в будущем на {abs(age_hours):.1f} ч"
            )
        elif age_hours > self.ttl_hours:
            stale_reasons.append(
                f"TTL превышен: {age_hours:.1f} ч > {self.ttl_hours:g} ч"
            )

        stale_reasons.extend(self._fingerprint_reasons(metadata))
        if key in TRADE_METRIC_REPORTS:
            stale_reasons.extend(self._sample_reasons(key, report, metadata))

        return ("STALE", stale_reasons) if stale_reasons else ("CURRENT", [])

    @staticmethod
    def _expected_metric_units(key: str) -> frozenset[str]:
        if key in CONTEXT_REPORTS:
            return frozenset({"CONTEXT", "COUNT", "SENTIMENT"})
        # Every report that votes from trade outcomes must first normalize to R.
        return frozenset({"R"})

    def _fingerprint_reasons(self, metadata: Mapping[str, Any]) -> list[str]:
        sources = metadata.get("source_files")
        fingerprints = metadata.get("source_file_fingerprints")
        hashes = metadata.get("source_file_hashes")
        if not isinstance(sources, list) or not sources:
            return ["source_files отсутствуют"]
        if not isinstance(fingerprints, Mapping) and not isinstance(hashes, Mapping):
            return ["fingerprints/hashes источников отсутствуют"]

        reasons: list[str] = []
        for raw_name in sources:
            name = str(raw_name)
            source_path = resolve_source_path(name, self.base_dir)
            expected_fingerprint = (
                fingerprints.get(name) if isinstance(fingerprints, Mapping) else None
            )
            if isinstance(expected_fingerprint, Mapping):
                current = source_file_fingerprint(source_path)
                if not fingerprints_match(expected_fingerprint, current):
                    reasons.append(f"source fingerprint изменился: {name}")
                continue

            expected_hash = hashes.get(name) if isinstance(hashes, Mapping) else None
            if not isinstance(expected_hash, str) or not expected_hash:
                reasons.append(f"нет fingerprint/hash источника {name}")
            elif source_file_sha256(source_path) != expected_hash:
                reasons.append(f"source hash изменился: {name}")
        return reasons

    def _sample_reasons(
        self,
        key: str,
        report: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> list[str]:
        reasons = []
        reported_closed = self.safe_int(metadata.get("closed_trades_total"))
        canonical_closed = self.safe_int(
            self.canonical_metrics.get("closed_trades")
        )
        if reported_closed != canonical_closed:
            reasons.append(
                "closed_trades_total не совпадает с canonical normalizer: "
                f"{reported_closed} != {canonical_closed}"
            )

        reported_complete = self.safe_int(metadata.get("complete_metrics_total"))
        canonical_complete = self.safe_int(
            self.canonical_metrics.get("metrics_trades")
        )
        if reported_complete != canonical_complete:
            reasons.append(
                "complete_metrics_total не совпадает с canonical normalizer: "
                f"{reported_complete} != {canonical_complete}"
            )
        body_closed, body_complete = self._body_sample(key, report)
        if body_closed is not None and body_closed != reported_closed:
            reasons.append(
                "payload closed sample не совпадает с metadata: "
                f"{body_closed} != {reported_closed}"
            )
        if body_complete is not None and body_complete != reported_complete:
            reasons.append(
                "payload complete sample не совпадает с metadata: "
                f"{body_complete} != {reported_complete}"
            )
        return reasons

    def _body_sample(
        self,
        key: str,
        report: Mapping[str, Any],
    ) -> tuple[int | None, int | None]:
        """Extract sample counts actually consumed by consensus rules."""
        if key in {"trade_loss", "trade_replay"}:
            sample = report.get("sample", {})
            if isinstance(sample, Mapping) and "closed_trades" in sample:
                complete = None
                metrics = report.get("metrics", {})
                if isinstance(metrics, Mapping) and "metrics_trades" in metrics:
                    complete = self.safe_int(metrics.get("metrics_trades"))
                return self.safe_int(sample.get("closed_trades")), complete

        if key == "strategy_lab":
            return None, self._strategy_lab_trade_count(report)
        if key == "hypothesis_lab":
            baseline = report.get("baseline", {})
            if isinstance(baseline, Mapping) and "trades" in baseline:
                return None, self.safe_int(baseline.get("trades"))
        if key == "trade_memory":
            portfolio = report.get("portfolio_metrics", {})
            if isinstance(portfolio, Mapping):
                closed = (
                    self.safe_int(portfolio.get("closed_trades"))
                    if "closed_trades" in portfolio
                    else None
                )
                complete = (
                    self.safe_int(portfolio.get("metrics_trades"))
                    if "metrics_trades" in portfolio
                    else None
                )
                return closed, complete
        if key == "news_statistics" and "sample_size" in report:
            return self.safe_int(report.get("sample_size")), None
        if key == "market_regime":
            regimes = report.get("trades_by_regime", {})
            if isinstance(regimes, Mapping):
                total = sum(
                    self.safe_int(row.get("trades"))
                    for row in regimes.values()
                    if isinstance(row, Mapping)
                )
                return total, None
        return None, None

    def _strategy_lab_trade_count(
        self,
        report: Mapping[str, Any],
    ) -> int | None:
        if "opportunities" in report:
            return self.safe_int(report.get("opportunities"))
        metrics = report.get("metrics", [])
        if not isinstance(metrics, list):
            return None
        baseline = next(
            (
                row for row in metrics
                if isinstance(row, Mapping)
                and str(row.get("strategy", "")).lower() == "current"
            ),
            None,
        )
        return self.safe_int(baseline.get("trades")) if baseline else None

    @staticmethod
    def modified_at(path: Path) -> str:
        """Return filesystem modification time in UTC."""
        if not path.exists():
            return ""
        return datetime.fromtimestamp(
            path.stat().st_mtime,
            tz=timezone.utc,
        ).isoformat()

    def report_groups(self) -> dict[str, list[dict[str, Any]]]:
        """Return accepted/stale/incompatible source summaries."""
        groups = {
            "accepted_reports": [],
            "stale_reports": [],
            "incompatible_reports": [],
            "missing_reports": [],
        }
        destination = {
            "CURRENT": "accepted_reports",
            "STALE": "stale_reports",
            "INCOMPATIBLE": "incompatible_reports",
            "MISSING": "missing_reports",
        }
        for status in self.source_status.values():
            groups[destination[status["state"]]].append({
                "file": status["file"],
                "reasons": list(status.get("reasons", [])),
                "metric_unit": status.get("metric_unit", ""),
            })
        return groups

    def incompatible_metric_units(self) -> list[dict[str, str]]:
        """Return reports rejected specifically for metric-unit mismatch."""
        rows = []
        for status in self.source_status.values():
            if any("metric_unit=" in reason for reason in status.get("reasons", [])):
                rows.append({
                    "file": str(status.get("file", "")),
                    "metric_unit": str(status.get("metric_unit", "")),
                })
        return rows

    def closed_trades(self) -> int:
        """Return the canonical normalized closed-trade sample."""
        return self.safe_int(self.canonical_metrics.get("closed_trades"))

    def context(self) -> dict[str, Any]:
        """Return context only from reports that passed freshness validation."""
        intelligence = self.reports.get("market_intelligence", {})
        dashboard = self.reports.get("dashboard", {})
        heatmap = self.reports.get("market_heatmap", {})
        return {
            "system_status": dashboard.get("system", {}).get("status", "UNKNOWN"),
            "market_regime": intelligence.get("market", {}).get(
                "regime", "Недостаточно данных"
            ),
            "news_sentiment": intelligence.get("market", {}).get(
                "news_sentiment", "Недостаточно данных"
            ),
            "heatmap_status": heatmap.get("status", "NO_DATA"),
            "near_setup": heatmap.get("summary", {}).get("NEAR SETUP", 0),
            "note": (
                "Market Intelligence, Heatmap и Dashboard используются как контекст "
                "только после freshness gate."
            ),
        }

    @staticmethod
    def safe_int(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0
