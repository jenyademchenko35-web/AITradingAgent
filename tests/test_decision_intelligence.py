import json
import tempfile
import unittest
from pathlib import Path

from decision_intelligence import build_report, format_telegram, run
from telegram_handlers import BOT_COMMANDS_V5


class DecisionIntelligenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); (self.root/"reports").mkdir()
        rows=[]
        for result,nets,direction in [(2,{"trend":4,"structure":2,"momentum":-1,"risk":1},"LONG"),(-1,{"trend":1,"structure":5,"momentum":3,"risk":-1},"SHORT")]:
            rows.append({"trade_id":str(len(rows)),"symbol":"BTC/USDT","direction":direction,"R":result,
                         "feature_contribution":{"modules":[{"feature":k,"weight":.25,"net_directional":v} for k,v in nets.items()]}})
        payload={"comparisons":rows,"calibration_pass":{"optimizer":{"recommendations":[{"feature":"structure","current":.25,"suggested":.2,"expected_profit_factor":{"current":.24,"suggested":.31},"confidence":"MEDIUM"}]}}}
        (self.root/"reports/decision_engine_v2.json").write_text(json.dumps(payload))
    def tearDown(self): self.tmp.cleanup()

    def test_trade_attribution_root_cause_and_accuracy(self):
        report=build_report(base_dir=self.root)
        self.assertEqual(report["sample"]["complete_decisions"],2)
        self.assertEqual(report["trades"][1]["root_cause"],"structure")
        self.assertEqual(report["decision_accuracy_pct"],50)
        self.assertEqual(report["recommendations"][0]["automatic_apply"],False)

    def test_report_and_formatters(self):
        report=run(base_dir=self.root); self.assertTrue((self.root/"decision_learning.json").exists())
        for view in ("learning","modules","accuracy","rootcause"): self.assertTrue(format_telegram(report,view))

    def test_commands_registered(self):
        commands={x.command for x in BOT_COMMANDS_V5}
        self.assertTrue({"learning","modules","accuracy","rootcause"} <= commands)


if __name__ == "__main__": unittest.main()
