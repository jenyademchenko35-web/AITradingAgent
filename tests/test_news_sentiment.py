from __future__ import annotations

import unittest

from news_observer.sentiment import classify_sentiment, sentiment_strength


class NewsSentimentTest(unittest.TestCase):
    def test_sentiment_classifies_bullish_and_bearish_text(self) -> None:
        bullish = classify_sentiment("Bitcoin rally and ETF approval trigger inflow")
        bearish = classify_sentiment("Exchange hack sparks selloff and investigation")

        self.assertEqual(bullish[0], "BULLISH")
        self.assertGreater(bullish[1], 0)
        self.assertEqual(bearish[0], "BEARISH")
        self.assertLess(bearish[1], 0)

    def test_sentiment_strength_stays_in_legacy_range(self) -> None:
        strength = sentiment_strength(0.8, 0.9, 0.7)
        self.assertGreaterEqual(strength, 1)
        self.assertLessEqual(strength, 5)


if __name__ == "__main__":
    unittest.main()
