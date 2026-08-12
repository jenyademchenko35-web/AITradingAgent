"""Golden values for unified R-based trade metrics."""

from __future__ import annotations

import unittest

from trade_metrics_normalizer import aggregate_trade_metrics


class UnifiedMetricsTest(unittest.TestCase):
    def test_pf_net_r_drawdown_and_incomplete_exclusion(self) -> None:
        rows = [
            {
                "symbol": "BTC/USDT",
                "direction": "LONG",
                "entry": "100",
                "stop_loss": "90",
                "exit_price": "120",
                "status": "WIN",
                "closed_at": "2026-07-13T01:00:00+00:00",
            },
            {
                "symbol": "ETH/USDT",
                "direction": "LONG",
                "entry": "100",
                "stop_loss": "90",
                "exit_price": "90",
                "status": "LOSS",
                "closed_at": "2026-07-13T02:00:00+00:00",
            },
            {
                "symbol": "SOL/USDT",
                "direction": "SHORT",
                "entry": "100",
                "stop_loss": "110",
                "exit_price": "80",
                "status": "WIN",
                "closed_at": "2026-07-13T03:00:00+00:00",
            },
            {
                "symbol": "ADA/USDT",
                "direction": "SHORT",
                "entry": "100",
                "stop_loss": "110",
                "status": "LOSS",
                "closed_at": "2026-07-13T04:00:00+00:00",
            },
        ]

        metrics = aggregate_trade_metrics(rows)

        self.assertEqual(metrics["closed_trades"], 4)
        self.assertEqual(metrics["metrics_trades"], 3)
        self.assertEqual(metrics["incomplete_metrics"], 1)
        self.assertEqual(metrics["profit_factor"], 4.0)
        self.assertEqual(metrics["net_r"], 3.0)
        self.assertEqual(metrics["max_drawdown_r"], 1.0)

    def test_empty_and_one_sided_results_never_return_infinite_or_nan_profit_factor(self) -> None:
        self.assertEqual(aggregate_trade_metrics([])["profit_factor"], 0.0)

        winner = {
            "symbol": "BTC/USDT", "direction": "LONG", "entry": "100",
            "stop_loss": "90", "exit_price": "110", "status": "WIN",
        }
        loser = {
            "symbol": "BTC/USDT", "direction": "LONG", "entry": "100",
            "stop_loss": "90", "exit_price": "90", "status": "LOSS",
        }
        self.assertEqual(aggregate_trade_metrics([winner])["profit_factor"], 0.0)
        self.assertEqual(aggregate_trade_metrics([loser])["profit_factor"], 0.0)

    def test_non_finite_prices_are_incomplete_and_excluded_from_metrics(self) -> None:
        for non_finite in ("NaN", "Infinity", "-Infinity"):
            metrics = aggregate_trade_metrics([{
                "symbol": "BTC/USDT", "direction": "LONG", "entry": "100",
                "stop_loss": "90", "exit_price": non_finite, "status": "WIN",
            }])
            self.assertEqual(metrics["closed_trades"], 1)
            self.assertEqual(metrics["metrics_trades"], 0)
            self.assertEqual(metrics["incomplete_metrics"], 1)
            self.assertEqual(metrics["net_r"], 0)


if __name__ == "__main__":
    unittest.main()
