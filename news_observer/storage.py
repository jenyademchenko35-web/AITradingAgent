"""Persistent artifacts for market news observer."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from report_metadata import build_report_metadata
from runtime_csv import append_row_atomic_or_locked

from .freshness import age_seconds
from .models import NewsItem, SourceFetchResult, SourceStatus


@dataclass(frozen=True)
class NewsArtifactPaths:
    """Project-local paths for observer artifacts."""

    report_json: Path
    current_csv: Path
    history_csv: Path
    summary_text: Path
    sources_json: Path
    health_json: Path


class NewsStorage:
    """Load and persist market news artifacts."""

    CURRENT_CSV_FIELDS = [
        "news_id",
        "id",
        "source_id",
        "source_name",
        "source_type",
        "fetched_at",
        "published_at",
        "time",
        "primary_asset",
        "coin",
        "source",
        "source_kind",
        "symbols",
        "categories",
        "url",
        "link",
        "title",
        "summary",
        "sentiment",
        "sentiment_score",
        "relevance_score",
        "freshness_score",
        "strength",
        "risk_score",
        "importance",
        "language",
        "duplicate_group_id",
        "parser_version",
        "supporting_sources",
    ]
    HISTORY_CSV_FIELDS = CURRENT_CSV_FIELDS + ["generated_at", "report_status"]

    def __init__(self, base_dir: Path | str) -> None:
        root = Path(base_dir).resolve()
        self.base_dir = root
        self.paths = NewsArtifactPaths(
            report_json=root / "market_news_feed.json",
            current_csv=root / "market_news_feed.csv",
            history_csv=root / "market_news_history.csv",
            summary_text=root / "market_news_summary.txt",
            sources_json=root / "market_news_sources.json",
            health_json=root / "market_news_health.json",
        )

    def load_report(self) -> dict[str, Any]:
        """Read cached report JSON when available."""
        return self._read_json(self.paths.report_json)

    def load_health(self) -> dict[str, Any]:
        """Read the previous health snapshot for cycle counters."""
        return self._read_json(self.paths.health_json)

    def load_cached_items(self) -> list[NewsItem]:
        """Return cached normalized items."""
        payload = self.load_report()
        raw_items = payload.get("news", [])
        if not isinstance(raw_items, list):
            return []
        result: list[NewsItem] = []
        for raw in raw_items:
            if isinstance(raw, Mapping):
                result.append(NewsItem.from_mapping(raw))
        return result

    def validate_cache(self) -> dict[str, Any]:
        """Validate existing cache status without network access."""
        payload = self.load_report()
        if not payload:
            return {
                "status": "NO_DATA",
                "news_count": 0,
                "generated_at": "",
                "age_seconds": None,
            }
        raw_items = payload.get("news", [])
        return {
            "status": str(payload.get("status") or "UNKNOWN"),
            "news_count": len(raw_items) if isinstance(raw_items, list) else 0,
            "generated_at": str(payload.get("generated_at") or ""),
            "age_seconds": age_seconds(payload.get("generated_at")),
        }

    def save(
        self,
        *,
        report: Mapping[str, Any],
        source_snapshot: Mapping[str, Any],
        health_snapshot: Mapping[str, Any],
        summary_text: str,
    ) -> None:
        """Persist all public artifacts."""
        news = [NewsItem.from_mapping(item) for item in report.get("news", []) if isinstance(item, Mapping)]
        self._atomic_write_json(self.paths.report_json, report)
        self._atomic_write_json(self.paths.sources_json, source_snapshot)
        self._atomic_write_json(self.paths.health_json, health_snapshot)
        self._atomic_write_text(self.paths.summary_text, summary_text)
        self._write_current_csv(news)
        self._append_history(news, str(report.get("generated_at") or ""), str(report.get("status") or ""))

    def build_metadata(
        self,
        *,
        generated_at: str,
        data_period_start: str,
        data_period_end: str,
        news_count: int,
        news_24h: int,
        cache_used: bool,
        status: str,
        source_results: Iterable[SourceFetchResult],
        duplicates_removed: int,
        last_success_at: str,
    ) -> dict[str, Any]:
        """Return report metadata extended with observer-specific fields."""
        source_files = [
            self.base_dir / "market_news_observer.py",
            self.base_dir / "news_observer" / "observer.py",
            self.base_dir / "news_observer" / "source_registry.py",
            self.base_dir / "news_observer" / "source_base.py",
            self.base_dir / "news_observer" / "fetcher.py",
            self.base_dir / "news_observer" / "models.py",
            self.base_dir / "news_observer" / "normalizer.py",
            self.base_dir / "news_observer" / "deduplicator.py",
            self.base_dir / "news_observer" / "sentiment.py",
            self.base_dir / "news_observer" / "relevance.py",
            self.base_dir / "news_observer" / "freshness.py",
            self.base_dir / "news_observer" / "health.py",
            self.base_dir / "news_observer" / "formatter.py",
            self.base_dir / "news_observer" / "storage.py",
        ]
        results = list(source_results)
        metadata = build_report_metadata(
            generator="market_news_observer.py",
            metric_unit="SENTIMENT",
            source_files=source_files,
            base_dir=self.base_dir,
            data_period_start=data_period_start,
            data_period_end=data_period_end,
            generated_at=generated_at,
            generator_version="2.0",
        )
        metadata.update(
            {
                "report_type": "MARKET_NEWS",
                "observer_version": "2.0",
                "status": status,
                "news_count": int(news_count),
                "news_total": int(news_count),
                "news_24h": int(news_24h),
                "duplicates_removed": int(duplicates_removed),
                "last_success_at": last_success_at,
                "freshness_ttl_hours": 2,
                "freshness_ttl": 7200,
                "source_health": {
                    item.name: item.status.value for item in results
                },
                "parser_versions": {
                    item.name: item.parser_version for item in results
                },
                "failed_sources": [
                    item.name
                    for item in results
                    if item.status in {
                        SourceStatus.TIMEOUT,
                        SourceStatus.HTTP_ERROR,
                        SourceStatus.PARSER_ERROR,
                        SourceStatus.FAILED,
                    }
                ],
                "source_hash": self._source_hash(results),
                "source_file_hashes": dict(metadata.get("source_file_fingerprints", {})),
                "cache_used": bool(cache_used),
                "history_file": self.paths.history_csv.name,
                "current_csv_file": self.paths.current_csv.name,
            }
        )
        return metadata

    @staticmethod
    def _source_hash(results: Iterable[SourceFetchResult]) -> str:
        """Return a stable source-set hash without exposing source URLs."""
        rows = sorted(
            (item.name, item.kind.value, item.parser_version)
            for item in results
            if item.kind.value != "LOCAL_CACHE"
        )
        payload = json.dumps(rows, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _write_current_csv(self, news: Iterable[NewsItem]) -> None:
        rows = []
        for item in news:
            payload = item.to_dict()
            payload["supporting_sources"] = "|".join(item.supporting_sources)
            payload["symbols"] = "|".join(item.assets)
            payload["categories"] = "|".join(item.categories)
            rows.append(payload)
        self._atomic_write_csv(self.paths.current_csv, self.CURRENT_CSV_FIELDS, rows)

    def _append_history(self, news: Iterable[NewsItem], generated_at: str, report_status: str) -> None:
        existing_ids = self._read_history_ids()
        for item in news:
            if item.id in existing_ids:
                continue
            row = item.to_dict()
            row["supporting_sources"] = "|".join(item.supporting_sources)
            row["symbols"] = "|".join(item.assets)
            row["categories"] = "|".join(item.categories)
            row["generated_at"] = generated_at
            row["report_status"] = report_status
            append_row_atomic_or_locked(self.paths.history_csv, self.HISTORY_CSV_FIELDS, row)
            existing_ids.add(item.id)

    def _read_history_ids(self) -> set[str]:
        path = self.paths.history_csv
        if not path.exists() or path.stat().st_size == 0:
            return set()
        try:
            with path.open("r", newline="", encoding="utf-8") as file:
                return {
                    str(row.get("id") or "").strip()
                    for row in csv.DictReader(file)
                    if row and str(row.get("id") or "").strip()
                }
        except (OSError, csv.Error):
            return set()

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists() or path.stat().st_size == 0:
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
        NewsStorage._atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _atomic_write_csv(path: Path, fieldnames: list[str], rows: list[Mapping[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=fieldnames)
                writer.writeheader()
                for row in rows:
                    writer.writerow({field: row.get(field, "") for field in fieldnames})
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
