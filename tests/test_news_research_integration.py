"""Research gates for the read-only Market News artifact."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from report_metadata import build_report_metadata
from research_orchestrator.artifact_registry import ArtifactRegistry
from research_orchestrator.evidence_gate import EvidenceBuilder
from research_orchestrator.models import ArtifactSpec


class NewsResearchIntegrationTest(TestCase):
    def _write_feed(self, root: Path, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        sources_path = root / "market_news_sources.json"
        sources_path.write_text('{"sources": []}', encoding="utf-8")
        metadata = build_report_metadata(
            generator="news_observer.observer.MarketNewsObserver",
            generator_version="2.0",
            metric_unit="SENTIMENT",
            source_files=[sources_path],
            base_dir=root,
            generated_at=now,
            data_period_start=now,
            data_period_end=now,
        )
        metadata.update({
            "last_success_at": now,
            "status": status,
            "news_24h": 1,
            "freshness_ttl_hours": 2,
        })
        payload = {
            "generated_at": now,
            "status": status,
            "metadata": metadata,
            "news": [{"title": "Bitcoin test", "symbols": ["BTC"]}],
            "summary": {
                "recent_24h": 1,
                "market_sentiment": "NEUTRAL",
            },
        }
        (root / "market_news_feed.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    @staticmethod
    def _spec() -> ArtifactSpec:
        return ArtifactSpec(
            "market_news_feed",
            "NEWS",
            "market_news_feed.json",
            False,
            "1.0",
            ("SENTIMENT", "CONTEXT"),
            2,
            "market_news_observer.py",
            False,
        )

    def test_fresh_feed_is_neutral_shadow_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_feed(root, "OK")
            registry = ArtifactRegistry(root, specs=(self._spec(),))

            record = registry.load_all()["market_news_feed"]
            evidence, _, _ = EvidenceBuilder().build(
                registry.accepted_payloads()
            )

            self.assertTrue(record.accepted)
            news = next(row for row in evidence if row.category == "NEWS")
            self.assertEqual(news.direction, "NEUTRAL")
            self.assertIn("Shadow News", news.notes)

    def test_stale_feed_is_insufficient_not_confirmation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_feed(root, "STALE")
            registry = ArtifactRegistry(root, specs=(self._spec(),))

            record = registry.load_all()["market_news_feed"]
            marker = EvidenceBuilder().build_news_context(
                record.payload,
                accepted=False,
                reasons=record.reasons,
            )

            self.assertEqual(record.status, "STALE")
            self.assertFalse(record.accepted)
            self.assertEqual(marker.direction, "INSUFFICIENT")
            self.assertEqual(marker.freshness_status, "STALE")
