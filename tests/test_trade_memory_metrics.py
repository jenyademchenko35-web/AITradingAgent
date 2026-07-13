"""Golden regression tests for Trade Memory subset metrics and linkage."""

from __future__ import annotations

from unittest import TestCase

from trade_memory import TradeMemory, calculate_subset_metrics


class TradeMemoryMetricsTest(TestCase):
    def test_eighteen_linked_similar_trades_have_nonzero_pf_and_net_r(self) -> None:
        matches = []
        for index in range(18):
            is_win = index < 5
            matches.append(
                {
                    "source_index": index + 1,
                    "result": "WIN" if is_win else "LOSS",
                    "closed_at": f"2026-07-13T{index:02d}:00:00+00:00",
                    "metrics_linked": True,
                    "match_quality": "SOURCE_ROW_ID",
                    "metrics_status": "COMPLETE",
                    "pnl_r": 1.5 if is_win else -0.5,
                }
            )

        metrics = calculate_subset_metrics(matches)

        self.assertEqual(metrics["matched_similar_total"], 18)
        self.assertEqual(metrics["metrics_complete_total"], 18)
        self.assertEqual(metrics["wins_with_metrics"], 5)
        self.assertEqual(metrics["losses_with_metrics"], 13)
        self.assertEqual(metrics["profit_factor"], 1.153846)
        self.assertEqual(metrics["net_r"], 1.0)
        self.assertNotEqual(metrics["profit_factor"], 0)
        self.assertNotEqual(metrics["net_r"], 0)

    def test_missing_linkage_is_unavailable_not_zero(self) -> None:
        metrics = calculate_subset_metrics(
            [
                {
                    "result": "WIN",
                    "metrics_linked": False,
                    "match_quality": "MISSING",
                    "metrics_status": "UNMATCHED",
                    "pnl_r": "",
                }
            ]
        )

        self.assertIsNone(metrics["profit_factor"])
        self.assertIsNone(metrics["net_r"])
        self.assertEqual(
            metrics["profit_factor_unavailable_reason"],
            "normalized metrics linkage missing",
        )

    def test_win_without_positive_r_emits_data_quality_warning(self) -> None:
        metrics = calculate_subset_metrics(
            [
                {
                    "result": "WIN",
                    "metrics_linked": True,
                    "match_quality": "TRADE_ID",
                    "metrics_status": "COMPLETE",
                    "pnl_r": "-1",
                }
            ]
        )

        self.assertTrue(
            any(
                "DATA_QUALITY_WARNING" in warning
                for warning in metrics["data_quality_warnings"]
            )
        )

    def test_source_row_id_link_rejects_stale_symbol(self) -> None:
        normalized = {
            "source_index": "7",
            "source_row_id": "7",
            "symbol": "SOL/USDT",
            "direction": "LONG",
            "opened_at": "2026-07-13T10:00:00+00:00",
            "closed_at": "2026-07-13T11:00:00+00:00",
            "metrics_status": "COMPLETE",
            "pnl_r": "1.2",
        }
        memory = TradeMemory.__new__(TradeMemory)
        memory.metrics_index = TradeMemory._build_metrics_index([normalized])

        linked, quality = memory._link_normalized_trade(
            {
                "symbol": "SOL/USDT",
                "direction": "LONG",
                "opened_at": "2026-07-13T10:00:00+00:00",
                "closed_at": "2026-07-13T11:00:00+00:00",
            },
            7,
        )
        self.assertIsNotNone(linked)
        self.assertEqual(quality, "SOURCE_ROW_ID")

        stale, stale_quality = memory._link_normalized_trade(
            {
                "symbol": "BTC/USDT",
                "direction": "LONG",
                "opened_at": "2026-07-13T10:00:00+00:00",
                "closed_at": "2026-07-13T11:00:00+00:00",
            },
            7,
        )
        self.assertIsNone(stale)
        self.assertEqual(stale_quality, "MISSING")


if __name__ == "__main__":
    import unittest

    unittest.main()
