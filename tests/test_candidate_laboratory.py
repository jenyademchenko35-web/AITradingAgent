import csv
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from candidate_laboratory import CandidateLaboratory


@dataclass
class Score:
    long: float
    short: float
    reason: str = ""


@dataclass
class Decision:
    direction: str
    signal: str
    score: int
    long_total: int
    short_total: int
    confidence: float
    quality: str


def decide(trend, structure, momentum, risk, weights):
    long = round(sum(x.long * weights[k] for x, k in zip(
        (trend, structure, momentum, risk), ("trend", "structure", "momentum", "risk")
    )))
    short = round(sum(x.short * weights[k] for x, k in zip(
        (trend, structure, momentum, risk), ("trend", "structure", "momentum", "risk")
    )))
    direction = "LONG" if long >= short else "SHORT"
    score = max(long, short)
    return Decision(direction, "SETUP" if score >= 20 else "NO TRADE", score, long, short, 85, "B")


class CandidateLaboratoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = root / "config.json"
        self.config.write_text(json.dumps({
            "LIVE_BASELINE": {"enabled": True, "shadow_only": True, "version": "1"},
        }))
        self.lab = CandidateLaboratory(
            decide, config_path=self.config,
            decisions_path=root / "decisions.csv", trades_path=root / "trades.csv",
        )
        self.snapshot = {
            "timestamp": "2026-01-01T00:00:00+00:00", "cycle_id": "c1",
            "snapshot_id": "s1", "symbol": "BTC/USDT", "timeframe": "1h",
            "current_price": 100, "atr": 5,
        }
        self.scores = [Score(60, 10), Score(20, 5), Score(20, 5), Score(10, 5)]
        self.weights = {"trend": .4, "structure": .2, "momentum": .25, "risk": .15}

    def tearDown(self):
        self.tmp.cleanup()

    def run_lab(self):
        live = decide(*self.scores, self.weights)
        return self.lab.run(
            snapshot=self.snapshot, live_decision=live,
            trend=self.scores[0], structure=self.scores[1],
            momentum=self.scores[2], risk=self.scores[3],
            live_weights=self.weights,
        )

    def test_baseline_repeats_live_and_snapshot_is_immutable(self):
        original = deepcopy(self.snapshot)
        live = decide(*self.scores, self.weights)
        result = self.run_lab()[0]
        self.assertEqual((result["direction"], result["decision"], result["score"]),
                         (live.direction, live.signal, live.score))
        self.assertEqual(self.snapshot, original)
        self.assertIs(result["shadow_only"], True)

    def test_baseline_repeats_real_live_decision_engine(self):
        from multi_timeframe_agent_v3 import DecisionEngine, EngineResult
        scores = [
            EngineResult(60, 10, ""), EngineResult(20, 5, ""),
            EngineResult(20, 5, ""), EngineResult(10, 5, ""),
        ]
        live = DecisionEngine.calculate(*scores, self.weights)
        self.lab.decision_fn = DecisionEngine.calculate
        result = self.lab.run(
            snapshot={**self.snapshot, "timestamp": "2026-01-01T01:00:00+00:00"},
            live_decision=live, trend=scores[0], structure=scores[1],
            momentum=scores[2], risk=scores[3], live_weights=self.weights,
        )[0]
        self.assertEqual(
            (result["direction"], result["decision"], result["score"], result["confidence"]),
            (live.direction, live.signal, live.score, live.confidence),
        )

    def test_duplicate_decisions_are_not_written(self):
        self.run_lab()
        self.run_lab()
        with self.lab.decisions_path.open() as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 1)

    def test_candidate_error_does_not_escape(self):
        self.lab.decision_fn = lambda *args: (_ for _ in ()).throw(RuntimeError("boom"))
        self.assertEqual(self.run_lab()[0]["status"], "ERROR")

    def test_shadow_has_no_live_execution_dependency(self):
        self.assertFalse(hasattr(self.lab, "open_trade"))
        self.run_lab()
        self.assertTrue(self.lab.trades_path.exists())

    def _close(self, high, low):
        self.run_lab()
        return self.lab.update_shadow_trades(symbol="BTC/USDT", high=high, low=low)[0]

    def test_shadow_trade_closes_tp(self):
        self.assertEqual(self._close(111, 99)["status"], "WIN")

    def test_shadow_trade_closes_sl(self):
        self.assertEqual(self._close(101, 94)["status"], "LOSS")

    def test_same_bar_sl_wins_conservatively(self):
        row = self._close(111, 94)
        self.assertEqual((row["status"], row["close_reason"]), ("LOSS", "SL"))


if __name__ == "__main__":
    unittest.main()
