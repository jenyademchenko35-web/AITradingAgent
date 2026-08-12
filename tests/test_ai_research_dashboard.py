import csv
import json
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

from ai_research_dashboard import (
    AIResearchDashboard,
    build_recommendation,
    calculate_health_score,
)


class AIResearchDashboardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_json(self, relative, payload):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_dashboard_forms_without_optional_files(self):
        dashboard = AIResearchDashboard(self.root)
        report = dashboard.build_report()
        text = dashboard.format_telegram(report)
        self.assertIn("📊 AI Research Dashboard", text)
        self.assertIn("No candidate data", text)
        self.assertIn("No news data", text)

    def test_health_score(self):
        report = {
            "trading": {"profit_factor": 0.8, "winrate": 20},
            "research": {"status": "WARNING"},
            "candidate": {"profit_factor": 0.9, "complete_trades": 20},
            "features": {"market_regime_field": 95, "unknown_market_regime": 35},
            "root_cause": {"causes": [{"severity": "HIGH"}]},
            "news": [{"status": "ONLINE"}, {"status": "NO_CONTENT"}],
        }
        health = calculate_health_score(report)
        self.assertEqual(health, {"score": 48, "label": "ATTENTION"})

    def test_candidate_recommendation(self):
        report = {
            "candidate": {"name": "Momentum Relaxed", "complete_trades": 37},
            "research": {"status": "OK"},
            "root_cause": {"causes": []},
        }
        recommendation = build_recommendation(report)
        self.assertIn("Continue collecting statistics", recommendation)
        self.assertIn("Momentum Relaxed", recommendation)

    def test_walk_forward_and_risk_recommendations(self):
        self.assertEqual(build_recommendation({
            "candidate": {}, "research": {"status": "WARNING"},
            "root_cause": {"causes": []},
        }), "Run Walk Forward Validation.")
        self.assertEqual(build_recommendation({
            "candidate": {}, "research": {"status": "OK"},
            "root_cause": {"causes": [{"name": "Risk", "severity": "HIGH"}]},
        }), "Review Risk Engine.")

    def test_data_integrity_blocks_legacy_walk_forward_recommendation(self):
        report = {
            "candidate": {}, "research": {"status": "WARNING"},
            "root_cause": {"causes": []},
            "research_v2": {
                "research_data_integrity": {
                    "gates": {
                        "ranking_allowed": True,
                        "walk_forward_allowed": False,
                        "promotion_allowed": False,
                    },
                    "checks": {
                        "UNRESOLVED_ATTRIBUTION": {
                            "historical_unresolved_joins": 22,
                            "current_pipeline_unresolved_joins": 0,
                        }
                    },
                }
            },
        }
        recommendation = build_recommendation(report)
        self.assertNotIn("Run Walk Forward Validation", recommendation)
        self.assertIn("fully joined research evidence", recommendation)
        self.assertIn("Data Integrity", recommendation)
        self.assertFalse(report["research_v2"]["research_data_integrity"]["gates"]["promotion_allowed"])

    def test_walk_forward_recommendation_is_preserved_when_integrity_allows_it(self):
        self.assertEqual(build_recommendation({
            "candidate": {}, "research": {"status": "WARNING"},
            "root_cause": {"causes": []},
            "research_v2": {
                "research_data_integrity": {
                    "gates": {"walk_forward_allowed": True, "promotion_allowed": False}
                }
            },
        }), "Run Walk Forward Validation.")

    def test_reads_news_and_candidate(self):
        self.write_json("agent_v3_stats.json", {"runs": 10})
        self.write_json("reports/candidate_laboratory.json", {
            "candidates": {
                "MOMENTUM_RELAXED": {
                    "profit_factor": 0.96, "complete_trades": 37,
                    "winrate": 32.4, "status": "COLLECTING",
                }
            }
        })
        self.write_json("market_news_feed.json", {
            "source_statuses": [
                {"name": "CoinDesk", "kind": "RSS", "status": "ONLINE"}
            ]
        })
        report = AIResearchDashboard(self.root).build_report()
        self.assertEqual(report["candidate"]["name"], "Momentum Relaxed")
        self.assertEqual(report["news"][0]["status"], "ONLINE")

    def test_existing_empty_metrics_candidate_is_still_shown(self):
        self.write_json("reports/candidate_laboratory.json", {
            "candidates": {
                "LIVE_BASELINE": {"profit_factor": None},
                "MOMENTUM_RELAXED": {
                    "profit_factor": None, "complete_trades": 0,
                    "total_decisions": 0,
                },
            }
        })
        report = AIResearchDashboard(self.root).build_report()
        self.assertEqual(report["candidate"]["name"], "Live Baseline")

    def test_legacy_candidate_list_and_fields(self):
        self.write_json("candidate_laboratory.json", {
            "results": [
                {"id": "OLD_A", "pf": 0.8, "closed": 10, "win_rate": 20},
                {"id": "OLD_B", "pf": 1.1, "closed": 12, "win_rate": 30},
            ]
        })
        report = AIResearchDashboard(self.root).build_report()
        self.assertEqual(report["candidate"]["name"], "Old B")
        self.assertEqual(report["candidate"]["profit_factor"], 1.1)
        self.assertEqual(report["candidate"]["complete_trades"], 12)

    def test_legacy_empty_202_parser_error_is_no_content(self):
        self.write_json("market_news_feed.json", {
            "source_statuses": [{
                "name": "Binance News", "kind": "RSS",
                "status": "PARSER_ERROR", "http_status": 202,
                "error": "no element found: line 1, column 0",
            }]
        })
        report = AIResearchDashboard(self.root).build_report()
        self.assertEqual(report["news"][0]["status"], "NO_CONTENT")
        self.assertNotIn("PARSER_ERROR", AIResearchDashboard(self.root).format_telegram(report))

    def test_new_no_content_status_is_preserved(self):
        self.write_json("market_news_sources.json", {
            "sources": [{
                "name": "Binance News", "kind": "RSS", "status": "NO_CONTENT",
            }]
        })
        report = AIResearchDashboard(self.root).build_report()
        self.assertEqual(report["news"][0]["status"], "NO_CONTENT")

    def test_feature_coverage_from_real_csv(self):
        path = self.root / "decision_features.csv"
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=[
                "snapshot_id", "atr", "adx", "volume", "market_regime",
                "session", "missing_features",
            ])
            writer.writeheader()
            writer.writerow({
                "snapshot_id": "1", "atr": "1", "adx": "2", "volume": "3",
                "market_regime": "UNKNOWN", "session": "ASIA",
                "missing_features": "",
            })
            writer.writerow({
                "snapshot_id": "2", "atr": "1", "adx": "2", "volume": "3",
                "market_regime": "TREND_UP", "session": "LONDON",
                "missing_features": "",
            })
        features = AIResearchDashboard(self.root).build_report()["features"]
        self.assertEqual(features["atr"], 100.0)
        self.assertEqual(features["market_regime_field"], 100.0)
        self.assertEqual(features["defined_market_regime"], 50.0)

    def test_dashboard_uses_root_cause_analyzer(self):
        expected = {
            "status": "OK", "causes": [{
                "name": "Trend", "percentage": 31.0, "severity": "HIGH",
            }]
        }
        with mock.patch("ai_research_dashboard.RootCauseAnalyzer") as analyzer:
            analyzer.return_value.build_report.return_value = expected
            report = AIResearchDashboard(self.root).build_report()
        analyzer.assert_called_once_with(self.root)
        self.assertIs(report["root_cause"], expected)

    def test_data_freshness_and_unknown(self):
        self.write_json("agent_v3_stats.json", {"runs": 1})
        timestamp = datetime.now(timezone.utc).timestamp() - 120
        path = self.root / "agent_v3_stats.json"
        path.touch()
        import os
        os.utime(path, (timestamp, timestamp))
        freshness = AIResearchDashboard(self.root).build_report()["freshness"]
        self.assertEqual(freshness["Agent Stats"], "2 min ago")
        self.assertEqual(freshness["Research Orchestrator"], "Unknown")


if __name__ == "__main__":
    unittest.main()
