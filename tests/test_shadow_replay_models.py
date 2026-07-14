"""Golden tests for Shadow Replay v2 execution and portfolio models."""

from __future__ import annotations

from unittest import TestCase

from shadow_replay.correlation import CorrelationEngine
from shadow_replay.execution_model import ExecutionAssumptions, ExecutionModel
from shadow_replay.latency_model import LatencyModel
from shadow_replay.metrics import compare_paths, metric_bundle
from shadow_replay.portfolio import PortfolioAssumptions, ReplayPortfolio


def trade(
    *,
    direction: str = "LONG",
    entry: float = 100.0,
    stop_loss: float = 90.0,
    exit_price: float = 120.0,
    symbol: str = "BTC/USDT",
    opened_at: str = "2026-01-01T00:00:00+00:00",
    closed_at: str = "2026-01-01T08:00:00+00:00",
) -> dict[str, object]:
    return {
        "source_index": 1,
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop_loss,
        "exit_price": exit_price,
        "risk_per_unit": abs(entry - stop_loss),
        "opened_at": opened_at,
        "closed_at": closed_at,
        "metrics_status": "COMPLETE",
    }


class ExecutionModelTest(TestCase):
    def test_fee_model_deducts_entry_and_exit_fees_in_r(self) -> None:
        model = ExecutionModel(ExecutionAssumptions(
            entry_fee_bps=10,
            exit_fee_bps=10,
            entry_slippage_bps=0,
            exit_slippage_bps=0,
            funding_rate_bps_per_8h=0,
            latency_adverse_bps_per_second=0,
        ))

        result = model.simulate(trade(), latency_seconds=0)

        self.assertAlmostEqual(result["ideal_gross_r"], 2.0)
        self.assertAlmostEqual(result["total_fee_r"], 0.022)
        self.assertAlmostEqual(result["effective_net_r"], 1.978)

    def test_slippage_model_is_adverse_for_long_and_short(self) -> None:
        assumptions = ExecutionAssumptions(
            entry_fee_bps=0,
            exit_fee_bps=0,
            entry_slippage_bps=10,
            exit_slippage_bps=10,
            funding_rate_bps_per_8h=0,
            latency_adverse_bps_per_second=0,
        )
        model = ExecutionModel(assumptions)

        long_result = model.simulate(trade(), latency_seconds=0)
        short_result = model.simulate(trade(
            direction="SHORT",
            stop_loss=110,
            exit_price=80,
        ), latency_seconds=0)

        self.assertLess(long_result["effective_net_r"], long_result["ideal_gross_r"])
        self.assertLess(short_result["effective_net_r"], short_result["ideal_gross_r"])
        self.assertLess(long_result["slippage_impact_r"], 0)
        self.assertLess(short_result["slippage_impact_r"], 0)

    def test_funding_model_pays_long_and_credits_short(self) -> None:
        assumptions = ExecutionAssumptions(
            entry_fee_bps=0,
            exit_fee_bps=0,
            entry_slippage_bps=0,
            exit_slippage_bps=0,
            funding_rate_bps_per_8h=10,
            latency_adverse_bps_per_second=0,
        )
        model = ExecutionModel(assumptions)

        long_result = model.simulate(trade(), latency_seconds=0)
        short_result = model.simulate(trade(
            direction="SHORT",
            stop_loss=110,
            exit_price=80,
        ), latency_seconds=0)

        self.assertLess(long_result["funding_r"], 0)
        self.assertEqual(long_result["funding_status"], "PAID")
        self.assertGreater(short_result["funding_r"], 0)
        self.assertEqual(short_result["funding_status"], "RECEIVED")

    def test_execution_impact_components_reconcile_to_effective_r(self) -> None:
        result = ExecutionModel().simulate(trade(), latency_seconds=3)
        combined_impact = sum(result[key] for key in (
            "fee_impact_r",
            "funding_impact_r",
            "slippage_impact_r",
            "latency_impact_r",
        ))
        self.assertAlmostEqual(
            result["ideal_gross_r"] + combined_impact,
            result["effective_net_r"],
            places=7,
        )


class LatencyModelTest(TestCase):
    def test_required_latency_scenarios_are_available(self) -> None:
        profiles = LatencyModel().scenario_profiles()
        self.assertEqual(
            [row["total_seconds"] for row in profiles],
            [0.0, 1.0, 3.0, 5.0, 10.0],
        )

    def test_longer_latency_never_improves_effective_r(self) -> None:
        assumptions = ExecutionAssumptions(
            entry_fee_bps=0,
            exit_fee_bps=0,
            entry_slippage_bps=0,
            exit_slippage_bps=0,
            funding_rate_bps_per_8h=0,
            latency_adverse_bps_per_second=1,
        )
        model = ExecutionModel(assumptions)
        zero = model.simulate(trade(), latency_seconds=0)
        ten = model.simulate(trade(), latency_seconds=10)
        self.assertLess(ten["effective_net_r"], zero["effective_net_r"])
        self.assertLess(ten["latency_impact_r"], 0)


class CorrelationModelTest(TestCase):
    def test_same_direction_major_pair_warns(self) -> None:
        result = CorrelationEngine().evaluate(
            {"symbol": "ETH/USDT", "direction": "LONG"},
            [{"symbol": "BTC/USDT", "direction": "LONG"}],
        )
        self.assertTrue(result["warning"])
        self.assertEqual(result["pairs"][0]["correlation"], 0.9)

    def test_opposite_direction_pair_does_not_warn(self) -> None:
        engine = CorrelationEngine()
        result = engine.evaluate(
            {"symbol": "ETH/USDT", "direction": "SHORT"},
            [{"symbol": "BTC/USDT", "direction": "LONG"}],
        )
        self.assertFalse(result["warning"])
        self.assertEqual(
            engine.describe()["assets"],
            ["BTC", "ETH", "SOL", "BNB", "DOGE"],
        )


class ReplayPortfolioTest(TestCase):
    def test_missing_close_time_does_not_block_following_trade(self) -> None:
        incomplete = trade(closed_at="not-a-timestamp")
        incomplete.update({
            "effective_entry": 100,
            "effective_net_r": 1,
            "ideal_gross_r": 1,
        })
        following = trade(
            symbol="ETH/USDT",
            opened_at="2026-01-01T01:00:00+00:00",
        )
        following.update({
            "source_index": 2,
            "effective_entry": 100,
            "effective_net_r": 1,
            "ideal_gross_r": 1,
        })
        portfolio = ReplayPortfolio(PortfolioAssumptions(
            max_open_positions=1,
            assumed_leverage=1,
        ))

        replayed, summary = portfolio.apply([incomplete, following])

        self.assertEqual(
            replayed[0]["portfolio_status"], "PORTFOLIO_TIME_INCOMPLETE"
        )
        self.assertFalse(replayed[0]["portfolio_allowed"])
        self.assertTrue(replayed[1]["portfolio_allowed"])
        self.assertNotIn("MAX_OPEN_POSITIONS", replayed[1]["portfolio_reasons"])
        self.assertNotIn("INSUFFICIENT_MARGIN", replayed[1]["portfolio_reasons"])
        self.assertEqual(summary["portfolio_time_incomplete"], 1)
        self.assertEqual(summary["portfolio_eligible_trades"], 1)
        self.assertEqual(summary["trades_skipped"], 0)
        metrics = compare_paths(replayed)
        self.assertEqual(metrics["ideal_all"]["trades"], 2)
        self.assertEqual(metrics["effective_execution_all"]["trades"], 2)
        self.assertEqual(metrics["effective_portfolio"]["trades"], 1)

    def test_third_overlapping_trade_is_skipped(self) -> None:
        rows = []
        for index, symbol in enumerate(("BTC/USDT", "ETH/USDT", "SOL/USDT"), 1):
            row = trade(symbol=symbol)
            row.update({
                "source_index": index,
                "effective_entry": 100,
                "effective_net_r": 1,
                "ideal_gross_r": 1,
            })
            rows.append(row)
        portfolio = ReplayPortfolio(PortfolioAssumptions(
            max_open_positions=2,
            assumed_leverage=10,
        ))

        replayed, summary = portfolio.apply(rows)

        self.assertEqual(summary["trades_executed"], 2)
        self.assertEqual(summary["trades_skipped"], 1)
        self.assertFalse(replayed[2]["portfolio_allowed"])
        self.assertIn("MAX_OPEN_POSITIONS", replayed[2]["portfolio_reasons"])


class ReplayMetricsTest(TestCase):
    def test_profit_factor_net_r_and_drawdown_use_r_only(self) -> None:
        result = metric_bundle(
            [
                {"value": 2.0},
                {"value": -1.0},
                {"value": -0.5},
            ],
            "value",
        )
        self.assertAlmostEqual(result["profit_factor"], 2 / 1.5, places=6)
        self.assertEqual(result["net_r"], 0.5)
        self.assertEqual(result["max_drawdown_r"], 1.5)
