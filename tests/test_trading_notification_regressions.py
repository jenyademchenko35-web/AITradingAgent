from __future__ import annotations

from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import AsyncMock, Mock, patch

import multi_timeframe_agent_v3 as agent


class TradingNotificationRegressionTest(TestCase):
    @staticmethod
    def _tf(*, ema20=110.0, ema50=100.0):
        return agent.TFData(
            close=100.0,
            rsi=55.0,
            ema20=ema20,
            ema50=ema50,
            atr=2.0,
            high20=110.0,
            low20=90.0,
            macd=1.0,
            macd_signal=0.5,
            price_position=50.0,
            trend_ema="BULLISH",
            trend_macd="BULLISH",
            high=101.0,
            low=99.0,
        )

    def _exercise(self, *, existing=(), higher_tf_bull=True,
                  portfolio_status="ALLOW", open_error=None):
        market = agent.MarketSnapshot(
            symbol="BTC/USDT",
            tf1h=self._tf(),
            tf4h=self._tf(ema20=110.0 if higher_tf_bull else 90.0),
            tf1d=self._tf(ema20=110.0 if higher_tf_bull else 90.0),
        )
        decision = agent.DecisionResult(
            direction="LONG",
            signal="SETUP",
            score=30,
            long_total=30,
            short_total=10,
            confidence=90.0,
            quality="A",
            summary="candidate",
            explanation="test",
        )
        engine = agent.EngineResult(long=10, short=0, reason="test")
        portfolio = {
            "status": portfolio_status,
            "reasons": [] if portfolio_status == "ALLOW" else ["MAX_OPEN_TRADES"],
        }
        open_mock = Mock(side_effect=open_error)
        mark_mock = Mock()

        with ExitStack() as stack:
            stack.enter_context(patch.object(agent, "load_market", return_value=market))
            stack.enter_context(patch.object(
                agent, "analyze_market",
                return_value=(decision, engine, engine, engine, engine, {}),
            ))
            stack.enter_context(patch.object(agent, "_research_lab_is_enabled", return_value=False))
            stack.enter_context(patch("feature_logger.build_feature_row", return_value={}))
            stack.enter_context(patch("feature_logger.FeatureLogger.log"))
            stack.enter_context(patch("candidate_laboratory.CandidateLaboratory.run", return_value=[]))
            stack.enter_context(patch(
                "candidate_shadow_tracker.CandidateShadowTracker.process_cycle",
                return_value={"opened": [], "closed": [], "open_count": 0},
            ))
            stack.enter_context(patch("candidate_report.build_reports"))
            stack.enter_context(patch.object(agent, "save_signal"))
            stack.enter_context(patch.object(agent, "save_decision_debug"))
            xai = Mock()
            xai.explain.return_value = {}
            xai.format_report.return_value = ""
            stack.enter_context(patch.object(agent, "ExplainableAI", return_value=xai))
            stack.enter_context(patch.object(agent.DecisionDiagnostics, "analyze", return_value={}))
            stack.enter_context(patch.object(agent.DecisionDiagnostics, "log"))
            stack.enter_context(patch.object(agent.DecisionDiagnostics, "format_report", return_value=""))
            for dry_run in (
                agent.PROTECTIVE_FILTER_DRY_RUN,
                agent.SL_QUALITY_PROTECTIVE_DRY_RUN,
                agent.CONFIDENCE_SL_QUALITY_D_DRY_RUN,
                agent.ADA_OPPORTUNITY_DRY_RUN,
                agent.DOGE_LINK_OPPORTUNITY_DRY_RUN,
                agent.LONG_REBOUND_OPPORTUNITY_DRY_RUN,
                agent.RELAXED_EDGE_DRY_RUN,
            ):
                stack.enter_context(patch.object(dry_run, "evaluate"))
            stack.enter_context(patch.object(agent, "is_setup_active", return_value=False))
            stack.enter_context(patch.object(agent, "get_open_trades", return_value=list(existing)))
            stack.enter_context(patch.object(
                agent.PORTFOLIO_MANAGER, "can_open_trade", return_value=portfolio,
            ))
            stack.enter_context(patch.object(agent, "open_trade", open_mock))
            stack.enter_context(patch.object(agent, "mark_setup_active", mark_mock))
            stack.enter_context(patch.object(agent, "save_setup_history"))
            stack.enter_context(patch.object(
                agent,
                "send_notification",
                AsyncMock(return_value=agent.NotificationResult(agent.NotificationStatus.SENT)),
            ))
            stack.enter_context(patch.object(agent, "log_notification_result"))
            stack.enter_context(patch.object(agent.LOGGER, "analysis_reports"))
            stack.enter_context(patch.object(agent.LOGGER, "timestamped"))
            stack.enter_context(patch.object(agent.LOGGER, "open_trade_exists"))
            stack.enter_context(patch.object(agent.LOGGER, "higher_tf_rejected"))
            stack.enter_context(patch.object(agent.LOGGER, "notification_sending"))

            if open_error is None:
                result = agent.analyze_symbol("BTC/USDT")
            else:
                with self.assertRaises(type(open_error)):
                    agent.analyze_symbol("BTC/USDT")
                result = None

        return result, open_mock, mark_mock

    def test_portfolio_rejection_does_not_record_cooldown(self):
        _, open_mock, mark_mock = self._exercise(portfolio_status="BLOCK")
        open_mock.assert_not_called()
        mark_mock.assert_not_called()

    def test_successful_open_records_cooldown(self):
        _, open_mock, mark_mock = self._exercise()
        open_mock.assert_called_once()
        mark_mock.assert_called_once_with("BTC_USDT_LONG")

    def test_existing_trade_does_not_record_cooldown(self):
        _, open_mock, mark_mock = self._exercise(existing=[{"symbol": "BTC/USDT"}])
        open_mock.assert_not_called()
        mark_mock.assert_not_called()

    def test_higher_timeframe_rejection_does_not_record_cooldown(self):
        _, open_mock, mark_mock = self._exercise(higher_tf_bull=False)
        open_mock.assert_not_called()
        mark_mock.assert_not_called()

    def test_open_trade_failure_does_not_record_cooldown(self):
        _, open_mock, mark_mock = self._exercise(open_error=RuntimeError("persist failed"))
        open_mock.assert_called_once()
        mark_mock.assert_not_called()

    def test_sent_log_requires_confirmed_success(self):
        with patch.object(agent.LOGGER, "notification_sent") as sent, \
                patch.object(agent.LOGGER, "timestamped") as timestamped:
            agent.log_notification_result(
                "BTC/USDT",
                agent.NotificationResult(agent.NotificationStatus.SENT),
            )
        sent.assert_called_once_with()
        timestamped.assert_not_called()

    def test_skipped_and_error_are_never_logged_as_sent(self):
        with patch.object(agent.LOGGER, "notification_sent") as sent, \
                patch.object(agent.LOGGER, "timestamped") as timestamped:
            agent.log_notification_result(
                "BTC/USDT",
                agent.NotificationResult(agent.NotificationStatus.SKIPPED, "duplicate"),
            )
            agent.log_notification_result(
                "BTC/USDT",
                agent.NotificationResult(agent.NotificationStatus.ERROR, "send_error", "timeout"),
            )
        sent.assert_not_called()
        self.assertEqual(timestamped.call_count, 2)
