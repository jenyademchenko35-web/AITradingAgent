import csv
import tempfile
import unittest
from pathlib import Path

from feature_logger import FeatureLogger, REGIMES, SESSIONS, classify_market_regime, classify_session


class FeatureLoggerTest(unittest.TestCase):
    def test_missing_fields_do_not_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "features.csv"
            FeatureLogger(path).log({
                "timestamp": "2026-01-01T00:00:00+00:00",
                "snapshot_id": "x", "symbol": "BTC/USDT",
            })
            with path.open() as stream:
                row = next(csv.DictReader(stream))
            self.assertIn("adx", row["missing_features"])

    def test_market_regime_is_valid(self):
        value = classify_market_regime({
            "current_price": 110, "ema50": 105, "ema200": 100, "adx": 25,
        })
        self.assertIn(value, REGIMES)

    def test_session_is_valid(self):
        self.assertIn(classify_session("2026-01-01T14:00:00+00:00"), SESSIONS)


if __name__ == "__main__":
    unittest.main()
