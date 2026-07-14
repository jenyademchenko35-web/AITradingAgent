"""Telegram News commands must remain file-only."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

import telegram_bot_v4 as bot


class NewsTelegramTest(TestCase):
    @staticmethod
    def _write_artifacts(root: Path) -> tuple[Path, Path, Path]:
        now = datetime.now(timezone.utc).isoformat()
        feed = root / "market_news_feed.json"
        health = root / "market_news_health.json"
        sources = root / "market_news_sources.json"
        feed.write_text(json.dumps({
            "generated_at": now,
            "status": "OK",
            "metadata": {
                "generated_at": now,
                "last_success_at": now,
                "news_24h": 1,
            },
            "summary": {
                "recent_24h": 1,
                "market_sentiment": "NEUTRAL",
            },
            "news": [{
                "title": "Bitcoin market update",
                "symbols": ["BTC"],
                "coin": "BTC",
                "sentiment": "NEUTRAL",
                "risk_score": 80,
                "importance": 4,
                "published_at": now,
                "time": now,
            }],
        }), encoding="utf-8")
        health.write_text(json.dumps({
            "status": "HEALTHY",
            "online_sources": 2,
            "failed_sources": 0,
        }), encoding="utf-8")
        sources.write_text(json.dumps({
            "sources": [
                {"source_name": "Fixture", "health_status": "ONLINE"},
            ],
        }), encoding="utf-8")
        return feed, health, sources

    def test_formatter_reads_files_without_network_or_subprocess(self) -> None:
        with TemporaryDirectory() as directory:
            feed, health, sources = self._write_artifacts(Path(directory))
            with (
                patch.object(bot, "MARKET_NEWS_FILE", feed),
                patch.object(bot, "MARKET_NEWS_HEALTH_FILE", health),
                patch.object(bot, "MARKET_NEWS_SOURCES_FILE", sources),
                patch("urllib.request.urlopen") as urlopen,
                patch("subprocess.run") as run,
            ):
                text = bot.format_news("BTC")

            self.assertIn("BTC", text)
            self.assertIn("Bitcoin market update", text)
            urlopen.assert_not_called()
            run.assert_not_called()

    def test_news_command_routes_context_argument_once(self) -> None:
        with TemporaryDirectory() as directory:
            feed, health, sources = self._write_artifacts(Path(directory))
            reply = AsyncMock()
            context = SimpleNamespace(args=["BTC"])
            with (
                patch.object(bot, "MARKET_NEWS_FILE", feed),
                patch.object(bot, "MARKET_NEWS_HEALTH_FILE", health),
                patch.object(bot, "MARKET_NEWS_SOURCES_FILE", sources),
                patch.object(bot, "reply", reply),
            ):
                asyncio.run(bot.news_command(None, context))

            reply.assert_awaited_once()
            self.assertIn("BTC", reply.await_args.args[1])

    def test_risks_and_stale_sections_use_ready_artifacts(self) -> None:
        with TemporaryDirectory() as directory:
            feed, health, sources = self._write_artifacts(Path(directory))
            with (
                patch.object(bot, "MARKET_NEWS_FILE", feed),
                patch.object(bot, "MARKET_NEWS_HEALTH_FILE", health),
                patch.object(bot, "MARKET_NEWS_SOURCES_FILE", sources),
            ):
                risks = bot.format_news("risks")
                stale = bot.format_news("stale")

            self.assertIn("Bitcoin market update", risks)
            self.assertIn("Статус данных: OK", stale)
