"""CLI and Telegram read-only interface tests."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import telegram_bot_v4
from adaptive_research.engine import cli_main
from adaptive_research.formatter import (
    format_overview,
    format_recommendations,
    format_stages,
    format_status,
)


REPORT = {
    "generated_at": "2026-07-14T12:00:00+00:00",
    "status": "SKIPPED",
    "trade_fingerprint": {"trade_count": 30},
    "pipeline": {
        "trigger": "UNCHANGED",
        "stages": [{
            "key": "metrics",
            "label": "Trade Metrics",
            "status": "SKIPPED",
            "duration_seconds": 0,
            "error": "закрытые сделки не изменились",
        }],
    },
    "artifact_gate": {"accepted_total": 5, "stale_total": 1, "ignored_total": 1},
    "recommendation": {
        "status": "COLLECT_MORE_DATA",
        "global_confidence_percent": 42.5,
        "leader": "Edge 16",
        "primary": "Продолжить Shadow Research.",
        "findings": [{
            "source": "lab",
            "finding": "Edge 16 требует наблюдения.",
            "confidence": 0.6,
        }],
    },
}

STATE = {
    "last_run": "2026-07-14T11:00:00+00:00",
    "last_check": "2026-07-14T12:00:00+00:00",
    "last_trade_count": 30,
    "pipeline_status": "SKIPPED",
}


class AdaptiveInterfacesTest(unittest.TestCase):
    def test_all_formatters_return_nonempty_read_only_text(self) -> None:
        texts = [
            format_overview(REPORT),
            format_status(REPORT, STATE),
            format_recommendations(REPORT),
            format_stages(REPORT),
        ]
        self.assertTrue(all(text.strip() for text in texts))
        self.assertTrue(all("LIVE" in text or "автомат" in text.lower() or "стад" in text.lower() for text in texts))

    def test_cli_status_does_not_run_engine(self) -> None:
        engine = MagicMock()
        engine.load_report.return_value = REPORT
        engine.load_state.return_value = STATE
        output = io.StringIO()
        with patch("adaptive_research.engine.AdaptiveResearchEngine", return_value=engine):
            with redirect_stdout(output):
                code = cli_main(["--status"])
        self.assertEqual(code, 0)
        engine.run.assert_not_called()
        self.assertIn("Adaptive Research / Статус", output.getvalue())

    def test_cli_only_delegates_selected_stage(self) -> None:
        engine = MagicMock()
        engine.run.return_value = {**REPORT, "status": "OK"}
        with patch("adaptive_research.engine.AdaptiveResearchEngine", return_value=engine):
            with redirect_stdout(io.StringIO()):
                code = cli_main(["--only", "replay"])
        self.assertEqual(code, 0)
        engine.run.assert_called_once_with(force=False, only="replay")

    def test_telegram_adaptive_commands_read_existing_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "adaptive_research_report.json"
            state_path = root / "adaptive_research_state.json"
            report_path.write_text(json.dumps(REPORT), encoding="utf-8")
            state_path.write_text(json.dumps(STATE), encoding="utf-8")
            with patch.object(telegram_bot_v4, "ADAPTIVE_RESEARCH_REPORT_FILE", report_path):
                with patch.object(telegram_bot_v4, "ADAPTIVE_RESEARCH_STATE_FILE", state_path):
                    overview = telegram_bot_v4.format_adaptive([])
                    status = telegram_bot_v4.format_adaptive(["status"])
                    recommendations = telegram_bot_v4.format_adaptive(["recommendations"])
                    stages = telegram_bot_v4.format_adaptive(["stages"])
        self.assertIn("Adaptive Research", overview)
        self.assertIn("Последний research-run", status)
        self.assertIn("Edge 16", recommendations)
        self.assertIn("Trade Metrics", stages)


if __name__ == "__main__":
    unittest.main()
