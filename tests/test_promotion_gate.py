import csv
import json
import tempfile
import unittest
from pathlib import Path

from promotion_gate import build_report, format_telegram, save_report
from telegram_handlers import BOT_COMMANDS_V5


class PromotionGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name); (self.root / "reports").mkdir()
        with (self.root / "trades.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["symbol","direction","entry_price","exit_price","result","risk_reward","opened_at","closed_at"])
            writer.writeheader(); writer.writerow({"symbol":"BTC/USDT","direction":"LONG","entry_price":1,"exit_price":2,"result":"WIN","risk_reward":2,"opened_at":"2026-01-01","closed_at":"2026-01-02"})

    def tearDown(self): self.tmp.cleanup()

    def write(self, path, value):
        target=self.root/path; target.parent.mkdir(parents=True,exist_ok=True); target.write_text(json.dumps(value))

    def test_not_ready_has_scores_and_reasons(self):
        self.write("shadow_replay_report.json", {"status":"INSUFFICIENT_DATA","metrics":{"effective_portfolio":{"profit_factor":.24,"winrate":20,"net_r":-3,"max_drawdown_r":10}}})
        self.write("reports/walk_forward.json", {"best":{"verdict":"CONTINUE_RESEARCH","stability_score":21}})
        self.write("reports/data_quality.json", {"coverage":{"coverage_pct":91}})
        report=build_report(base_dir=self.root)
        self.assertEqual(report["status"],"NOT_READY"); self.assertLess(report["shadow_score"],100)
        self.assertEqual(set(report["checks"]), {"profit_factor","winrate","net_r","max_drawdown","data_coverage","walk_forward","replay","stability","sample_size"})
        self.assertIn("PF below target", {x["reason"] for x in report["reasons"]})
        self.assertIn("NOT_READY", format_telegram(report))

    def test_history_is_appended(self):
        report={"generated_at":"2026-01-01T00:00:00+00:00","status":"NOT_READY","shadow_score":41,"metrics":{}}
        save_report(report,base_dir=self.root); report["generated_at"]="2026-01-02T00:00:00+00:00"; report["shadow_score"]=53; save_report(report,base_dir=self.root)
        history=json.loads((self.root/"reports/promotion_gate_history.json").read_text())["history"]
        self.assertEqual([41,53],[x["shadow_score"] for x in history])

    def test_ready_command_registered(self):
        self.assertIn("ready", {command.command for command in BOT_COMMANDS_V5})


if __name__ == "__main__": unittest.main()
