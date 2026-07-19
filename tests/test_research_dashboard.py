"""Tests for the unified read-only Research Dashboard v1."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from research_dashboard import (
    ALLOWED_RECOMMENDATIONS,
    SOURCE_SPECS,
    build_report,
    calculate_final_score,
    detect_conflicts,
    format_telegram,
    load_sources,
    save_report,
)


NOW = datetime(2026, 7, 19, 18, 0, tzinfo=timezone.utc)


def write_json(base: Path, relative: str, payload: dict) -> None:
    path = base / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def source_payloads(base: Path) -> None:
    generated = NOW.isoformat()
    write_json(
        base,
        "trade_metrics_audit.json",
        {
            "generated_at": generated,
            "metrics": {
                "closed_trades": 51,
                "metrics_trades": 46,
                "incomplete_metrics": 5,
                "winrate": 19.57,
                "profit_factor": 0.417,
                "net_r": -27.81,
                "average_r": -0.6046,
                "max_drawdown_r": 29.9,
            },
        },
    )
    write_json(
        base,
        "hypothesis_report.json",
        {
            "generated_at": generated,
            "status": "OK",
            "metrics": [
                {
                    "hypothesis": "baseline",
                    "trades": 46,
                    "profit_factor": 0.417,
                    "net_r": -27.81,
                    "verdict": "BASELINE",
                },
                {
                    "hypothesis": "Edge >= 20",
                    "trades": 40,
                    "profit_factor": 1.25,
                    "net_r": 5.0,
                    "winrate": 47.5,
                    "max_drawdown_r": 4.0,
                    "verdict": "STRONG",
                },
                {
                    "hypothesis": "Tiny Sample",
                    "trades": 5,
                    "profit_factor": 3.0,
                    "net_r": 6.0,
                    "winrate": 80,
                    "max_drawdown_r": 1.0,
                    "verdict": "PROMISING",
                },
            ],
        },
    )
    write_json(
        base,
        "reports/walk_forward.json",
        {
            "generated_at": generated,
            "status": "OK",
            "hypotheses": [
                {
                    "hypothesis": "Edge >= 20",
                    "total_test_trades": 35,
                    "average_pf": 1.1,
                    "average_net_r": 2.5,
                    "stability_score": 70,
                    "stability": "Partially Stable",
                    "confidence": "MEDIUM",
                    "verdict": "CONTINUE_RESEARCH",
                },
                {
                    "hypothesis": "Tiny Sample",
                    "total_test_trades": 5,
                    "average_pf": 3.0,
                    "average_net_r": 6.0,
                    "stability_score": 90,
                    "stability": "Stable",
                    "confidence": "LOW",
                    "verdict": "CONTINUE_RESEARCH",
                },
            ],
        },
    )
    write_json(
        base,
        "experiment_promotion_report.json",
        {
            "generated_at": generated,
            "status": "OK",
            "candidates": [
                {
                    "candidate": "Edge >= 20",
                    "promotion_status": "OBSERVE_ONLY",
                    "confidence": 65,
                    "metrics": {"trades": 40, "profit_factor": 1.25, "net_r": 5.0},
                }
            ],
        },
    )
    write_json(
        base,
        "research_orchestrator_report.json",
        {
            "generated_at": generated,
            "status": "OK",
            "canonical_metrics": {},
            "hypotheses": [
                {
                    "hypothesis": "Edge >= 20",
                    "status": "OBSERVATION_ONLY",
                    "confidence": "MEDIUM",
                    "metrics": {"trades": 40, "profit_factor": 1.25, "net_r": 5.0},
                    "contradicting_sources": [],
                }
            ],
            "conflicts": [],
        },
    )
    write_json(
        base,
        "adaptive_research_report.json",
        {
            "generated_at": generated,
            "status": "OK",
            "recommendation": {"global_confidence_percent": 66},
        },
    )
    write_json(
        base,
        "shadow_replay_report.json",
        {"generated_at": generated, "status": "OK", "metrics": {}},
    )


class ResearchDashboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        source_payloads(self.base)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self):
        return build_report(base_dir=self.base, now=NOW, history_dir=self.base / "reports/history")

    def test_loads_all_available_sources_and_optional_risk_is_missing(self) -> None:
        payloads, health, fingerprint = load_sources(self.base, now=NOW)
        self.assertIn("walk_forward", payloads)
        self.assertIn("strategy_lab", payloads)
        self.assertIn("baseline", payloads)
        self.assertEqual("MISSING", health["risk_filter_audit"]["status"])
        self.assertTrue(fingerprint)

    def test_missing_report_does_not_crash(self) -> None:
        (self.base / "adaptive_research_report.json").unlink()
        report = self.build()
        self.assertEqual("MISSING", report["source_health"]["adaptive_research"]["status"])
        self.assertIn(report["main_recommendation"], ALLOWED_RECOMMENDATIONS)

    def test_malformed_json_is_isolated_in_source_health(self) -> None:
        (self.base / "hypothesis_report.json").write_text("{bad", encoding="utf-8")
        report = self.build()
        self.assertEqual("MALFORMED", report["source_health"]["strategy_lab"]["status"])
        self.assertEqual("DEGRADED", report["system_research_status"]["status"])

    def test_hypothesis_ranking_contains_required_fields(self) -> None:
        ranking = self.build()["hypothesis_ranking"]
        self.assertEqual("Edge >= 20", ranking[0]["name"])
        for field in (
            "rank",
            "trades",
            "profit_factor",
            "net_r",
            "winrate",
            "stability_score",
            "confidence",
            "lab_verdict",
            "walk_forward_verdict",
            "promotion_status",
            "conflict_count",
            "final_research_score",
            "score_breakdown",
        ):
            self.assertIn(field, ranking[0])

    def test_small_sample_penalty_prevents_pf_from_dominating(self) -> None:
        mature = {
            "profit_factor": 1.2,
            "net_r": 4,
            "trades": 40,
            "stability_score": 70,
            "stability": "Partially Stable",
            "confidence": "MEDIUM",
            "drawdown_r": 5,
        }
        tiny = {**mature, "profit_factor": 3.0, "trades": 5}
        mature_score, _ = calculate_final_score(mature)
        tiny_score, breakdown = calculate_final_score(tiny)
        self.assertGreater(mature_score, tiny_score)
        self.assertGreater(breakdown["penalties"]["small_sample"], 0)

    def test_negative_net_r_has_strong_penalty(self) -> None:
        positive = {"profit_factor": 1.2, "net_r": 5, "trades": 40, "stability_score": 70, "confidence": "MEDIUM"}
        negative = {**positive, "net_r": -5}
        positive_score, _ = calculate_final_score(positive)
        negative_score, breakdown = calculate_final_score(negative)
        self.assertGreaterEqual(positive_score - negative_score, 15)
        self.assertGreater(breakdown["penalties"]["negative_net_r"], 0)

    def test_unstable_walk_forward_is_penalized(self) -> None:
        stable = {"profit_factor": 1.2, "net_r": 5, "trades": 40, "stability_score": 70, "stability": "Stable", "confidence": "MEDIUM"}
        unstable = {**stable, "stability": "Unstable"}
        stable_score, _ = calculate_final_score(stable)
        unstable_score, breakdown = calculate_final_score(unstable)
        self.assertGreaterEqual(stable_score - unstable_score, 15)
        self.assertEqual(16, breakdown["penalties"]["unstable_walk_forward"])

    def test_conflict_detection_for_strong_but_unstable(self) -> None:
        records = {
            "Candidate": {
                "lab_verdict": "STRONG",
                "walk_forward_verdict": "CONTINUE_RESEARCH",
                "stability": "Unstable",
                "promotion_status": "OBSERVE_ONLY",
                "trades": 40,
                "profit_factor": 1.2,
                "net_r": 2,
            }
        }
        conflicts = detect_conflicts(records, {})
        self.assertTrue(any(row["severity"] == "HIGH" for row in conflicts))

    def test_history_deduplicates_same_input_fingerprint(self) -> None:
        report = self.build()
        history = self.base / "reports/history"
        first = save_report(
            report,
            dashboard_json=self.base / "reports/dashboard.json",
            summary_path=self.base / "reports/summary.txt",
            history_dir=history,
        )
        second = save_report(
            report,
            dashboard_json=self.base / "reports/dashboard.json",
            summary_path=self.base / "reports/summary.txt",
            history_dir=history,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(1, len(list(history.glob("*.json"))))

    def test_telegram_formatter_supports_all_sections(self) -> None:
        report = self.build()
        for section in ("", "hypotheses", "conflicts", "status", "history"):
            text = format_telegram(report, section)
            self.assertTrue(text.strip())
            self.assertIn("Research Dashboard", text)

    def test_recommendation_is_allowed_and_application_is_forbidden(self) -> None:
        report = self.build()
        self.assertIn(report["main_recommendation"], ALLOWED_RECOMMENDATIONS)
        self.assertFalse(report["restrictions"]["automatic_application"])
        self.assertTrue(report["restrictions"]["live_unchanged"])
        self.assertTrue(report["restrictions"]["decision_engine_unchanged"])


if __name__ == "__main__":
    unittest.main()
