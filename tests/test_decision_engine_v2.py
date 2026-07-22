from __future__ import annotations

import hashlib
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest import TestCase

from decision_engine_v2 import DecisionEngineV2, format_decision_v2

PROJECT_ROOT=Path(__file__).resolve().parents[1]


class FakeRegistry:
    def get_complete_trades(self): return []


def weights(root: Path) -> Path:
    path=root/"weights.json"; path.write_text(json.dumps({
        "version":"test","acceptance":{"minimum_calibrated_confidence":80,"minimum_calibrated_score":25},
        "confidence_calibration":{"weak_separation_penalty":-2.5,"high_confidence_penalty":-.5,"winning_losing_gap_reference":5},
        "predictive_power_multipliers":{"NONE":.25,"LOW":.5,"MEDIUM":.75,"HIGH":1},
        "score_adjustments":{"hour_07":-1,"quality_B":-.75,"score_25_26":-.5,"confidence_90_plus":-.5,"weekday_Monday":-.25,"weekday_Tuesday":-.25,"symbol_SOL":-.25},
        "limits":{"maximum_absolute_confidence_change":5,"maximum_absolute_score_change":3}}),encoding="utf-8"); return path


def feature_rows():
    rows=[]
    for i in range(12):
        rows.append({"trade_id":f"T{i}","R":2 if i in {3,7,11} else -1,"opened_at":f"2026-06-{i+1:02d}T07:00:00+00:00",
                     "symbol":"SOL/USDT" if i<6 else "BTC/USDT","direction":"SHORT","confidence":92,
                     "score":26,"quality":"B" if i<8 else "A","hour":7 if i<6 else 19,"weekday":"Monday" if i<4 else "Wednesday",
                     "trend_long":10,"trend_short":50,"structure_long":10,"structure_short":0,
                     "momentum_long":14,"momentum_short":7,"risk_long":0,"risk_short":0})
    return rows


class DecisionEngineV2Test(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); root=Path(self.temp.name); (root/"reports").mkdir()
        self.root=root; self.v1_path=PROJECT_ROOT/"multi_timeframe_agent_v3.py"; self.v1_hash=hashlib.sha256(self.v1_path.read_bytes()).hexdigest()
        quality={"dataset_fingerprint":"test","winning_profile":{"average_confidence":92.86},"losing_profile":{"average_confidence":91.92},
                 "feature_ranking":[{"feature":name,"predictive_power":"LOW"} for name in ("confidence","hour","weekday","symbol")]+[{"feature":name,"predictive_power":"NONE"} for name in ("quality","score")]}
        self.engine=DecisionEngineV2(base_dir=root,registry=FakeRegistry(),weights_path=weights(root),feature_rows=feature_rows(),signal_quality_report=quality,
            report_path=root/"reports/decision_engine_v2.json",summary_path=root/"reports/decision_engine_v2_summary.txt",history_dir=root/"reports/decision_engine_v2_history")

    def tearDown(self): self.temp.cleanup()

    def test_confidence_calibration_preserves_raw_and_is_bounded(self):
        result=self.engine.calibrate_confidence(92)
        self.assertEqual(result["raw_confidence"],92); self.assertLess(result["calibrated_confidence"],92)
        self.assertGreaterEqual(result["calibrated_confidence"],0); self.assertTrue(result["calibration_reason"])

    def test_unknown_confidence_is_preserved(self):
        result=self.engine.calibrate_confidence("UNKNOWN")
        self.assertIsNone(result["raw_confidence"]); self.assertIsNone(result["calibrated_confidence"])

    def test_score_calibration_is_transparent_and_does_not_mutate(self):
        signal={"score":26,"confidence":92,"quality":"B","hour":7,"weekday":"Monday","symbol":"SOL/USDT"}; original=deepcopy(signal)
        result=self.engine.calibrate_score(signal)
        self.assertEqual(result["raw_score"],26); self.assertLess(result["calibrated_score"],26)
        self.assertGreater(len(result["score_adjustments"]),1); self.assertEqual(signal,original)

    def test_explainability_and_v1_v2_comparison(self):
        result=self.engine.evaluate({"score":26,"confidence":92,"quality":"B","hour":7,"weekday":"Monday","symbol":"SOL/USDT","accepted_by_v1":True})
        self.assertTrue(result["accepted_by_v1"]); self.assertIn("confidence",result["explanation"])
        self.assertIn("score",result["explanation"]); self.assertIsInstance(result["agreement"],bool)

    def test_report_contains_replay_walk_forward_and_allowed_status(self):
        report=self.engine.build_report()
        self.assertIn(report["status"],{"EXPERIMENTAL","PROMISING","READY_FOR_AB","REJECT"})
        self.assertIn("v1",report["shadow_replay"]); self.assertIn("v2",report["shadow_replay"])
        self.assertIn("windows",report["walk_forward"]); self.assertEqual(report["signals"]["compared"],12)
        calibration=report["calibration_pass"]
        self.assertEqual(calibration["version"],"2.1"); self.assertEqual(len(calibration["replay_matrix"]),17)
        self.assertIn("class_overlap",calibration["confidence_distribution"])
        self.assertFalse(calibration["optimizer"]["automatic_apply"])

    def test_feature_contributions_are_directional_and_transparent(self):
        contribution=self.engine.feature_contributions(feature_rows()[0])
        self.assertEqual(contribution["direction"],"SHORT")
        self.assertEqual({row["feature"] for row in contribution["modules"]},{"trend","structure","momentum","risk"})
        self.assertEqual(contribution["raw_score"],26); self.assertLess(contribution["calibrated_score"],26)

    def test_diff_report_classifies_outcomes_and_preserves_weights(self):
        original=deepcopy(self.engine.strategy_weights); report=self.engine.build_report()["calibration_pass"]
        self.assertTrue(all(row["outcome"] in {"WIN","LOSS","BREAK_EVEN"} for row in report["differences"]))
        self.assertEqual(self.engine.strategy_weights,original)
        for variant in report["replay_matrix"]:
            self.assertAlmostEqual(sum(variant["weights"].values()),1.0,places=6)

    def test_reports_and_history_deduplicate(self):
        _,first=self.engine.write_reports(); _,second=self.engine.write_reports()
        self.assertTrue(first); self.assertFalse(second); self.assertTrue(self.engine.report_path.exists()); self.assertTrue(self.engine.summary_path.exists()); self.assertTrue(self.engine.diff_report_path.exists())
        self.assertEqual(len(list(self.engine.history_dir.glob("*.json"))),1)

    def test_telegram_formatter_and_registration(self):
        report=self.engine.build_report()
        for view in ("","compare","report","diff","weights","optimizer"): self.assertIn("DecisionEngine v2",format_decision_v2(report,view))
        from telegram_handlers import BOT_COMMANDS_V5
        import telegram_bot_v4
        self.assertIn("decisionv2",{x.command for x in BOT_COMMANDS_V5}); self.assertTrue(callable(telegram_bot_v4.decisionv2_command))

    def test_v1_file_is_not_changed(self):
        self.engine.build_report()
        self.assertEqual(hashlib.sha256(self.v1_path.read_bytes()).hexdigest(),self.v1_hash)

    def test_no_live_or_automatic_promotion(self):
        report=self.engine.build_report()
        self.assertIn("NO_LIVE_IMPORT",report["restrictions"]); self.assertIn("NO_AUTOMATIC_PROMOTION",report["restrictions"])
