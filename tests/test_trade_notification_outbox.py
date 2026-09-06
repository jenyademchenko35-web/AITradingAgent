from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import AsyncMock, Mock, patch

import multi_timeframe_agent_v3 as agent
import notification_manager as notifications
import trade_notification_outbox as outbox_module
from trade_notification_outbox import (
    DELIVERED,
    PENDING,
    PERMANENT_SKIP,
    RETRYABLE_ERROR,
    SENDING,
    OutboxCorruptError,
    OutboxPersistError,
    RECOVERY_INTENT_KEY,
    TradeNotificationOutbox,
    notification_id_for_trade,
    recovery_intent,
)


class TradeNotificationOutboxTest(TestCase):
    def setUp(self):
        self.singleton_patch = patch.object(
            agent, "_AGENT_SINGLETON_LOCK", Mock(acquired=True),
        )
        self.singleton_patch.start()
        self.addCleanup(self.singleton_patch.stop)
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.clock = [1_000.0]
        self.path = Path(self.temporary.name) / "trade_notification_outbox.json"
        self.outbox = TradeNotificationOutbox(
            self.path,
            now=lambda: self.clock[0],
            base_backoff_seconds=10,
            max_backoff_seconds=40,
        )

    @staticmethod
    def trade(trade_id="LIVE-1", *, symbol="BTC/USDT"):
        metadata = {
            RECOVERY_INTENT_KEY: recovery_intent(
                fingerprint=f"fingerprint-{trade_id}",
                text=f"open {symbol}",
            )
        }
        return {
            "trade_id": trade_id,
            "symbol": symbol,
            "direction": "LONG",
            "opened_at": "2026-09-05T12:00:00",
            "research_metadata_json": json.dumps(metadata),
        }

    def enqueue(self, trade_id="LIVE-1"):
        item = self.outbox.enqueue_from_trade(self.trade(trade_id))
        self.assertIsNotNone(item)
        return item

    def test_successful_open_intent_is_durable_before_delivery(self):
        item = self.enqueue()
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["items"][item["notification_id"]]["state"], PENDING)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(item["attempt_count"], 0)

    def test_crash_after_trade_before_outbox_is_reconciled_after_restart(self):
        self.assertFalse(self.path.exists())
        restarted = TradeNotificationOutbox(self.path, now=lambda: self.clock[0])
        self.assertEqual(restarted.reconcile_trades([self.trade()]), 1)
        item = restarted.get(notification_id_for_trade("LIVE-1"))
        self.assertEqual(item["state"], PENDING)

    def test_duplicate_cycle_does_not_create_duplicate_item(self):
        first = self.enqueue()
        self.assertEqual(self.outbox.reconcile_trades([self.trade()]), 0)
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(state["items"]), [first["notification_id"]])

    def test_existing_trade_without_recovery_marker_creates_no_intent(self):
        trade = self.trade()
        trade["research_metadata_json"] = "{}"
        self.assertEqual(self.outbox.reconcile_trades([trade]), 0)
        self.assertFalse(self.path.exists())

    def test_sending_after_crash_becomes_retryable(self):
        item = self.enqueue()
        self.assertEqual(self.outbox.claim(item["notification_id"])["state"], SENDING)
        self.assertEqual(self.outbox.recover_interrupted(), 1)
        recovered = self.outbox.get(item["notification_id"])
        self.assertEqual(recovered["state"], RETRYABLE_ERROR)
        self.assertEqual(self.outbox.due_ids(), [item["notification_id"]])

    def test_cycle_recovery_processes_interrupted_item_safely(self):
        item = self.enqueue()
        self.outbox.claim(item["notification_id"])
        bot = Mock(send_message=AsyncMock(return_value=None))
        with patch.object(agent, "TRADE_NOTIFICATION_OUTBOX", self.outbox), \
                patch.object(agent, "get_all_trades", return_value=[]), \
                patch.object(agent, "BOT_TOKEN", "token"), \
                patch.object(agent, "load_chat_id", return_value=1), \
                patch.object(agent, "Bot", return_value=bot), \
                patch.object(agent, "is_duplicate", return_value=False), \
                patch.object(agent, "mark_as_sent"), \
                patch.object(agent, "log_notification_result") as logged:
            agent.process_notification_outbox()
        bot.send_message.assert_awaited_once()
        logged.assert_called_once()
        self.assertEqual(self.outbox.get(item["notification_id"])["state"], DELIVERED)

    def test_send_error_remains_retryable_with_backoff(self):
        item = self.enqueue()
        bot = Mock(send_message=AsyncMock(side_effect=OSError("network down")))
        with patch.object(agent, "TRADE_NOTIFICATION_OUTBOX", self.outbox), \
                patch.object(agent, "BOT_TOKEN", "token"), \
                patch.object(agent, "load_chat_id", return_value=1), \
                patch.object(agent, "Bot", return_value=bot), \
                patch.object(agent, "is_duplicate", return_value=False):
            result = asyncio.run(agent.send_outbox_notification(item["notification_id"]))
        self.assertEqual(result.status, agent.NotificationStatus.ERROR)
        stored = self.outbox.get(item["notification_id"])
        self.assertEqual(stored["state"], RETRYABLE_ERROR)
        self.assertEqual(stored["attempt_count"], 1)
        self.assertEqual(stored["next_attempt_at"], 1_010.0)

    def test_retry_later_sent_and_delivered_persisted(self):
        item = self.enqueue()
        claimed = self.outbox.claim(item["notification_id"])
        self.outbox.mark_retryable(claimed["notification_id"], "timeout")
        self.clock[0] = 1_010.0
        bot = Mock(send_message=AsyncMock(return_value=None))
        with patch.object(agent, "TRADE_NOTIFICATION_OUTBOX", self.outbox), \
                patch.object(agent, "BOT_TOKEN", "token"), \
                patch.object(agent, "load_chat_id", return_value=1), \
                patch.object(agent, "Bot", return_value=bot), \
                patch.object(agent, "is_duplicate", return_value=False), \
                patch.object(agent, "mark_as_sent") as acknowledged:
            result = asyncio.run(agent.send_outbox_notification(item["notification_id"]))
        self.assertEqual(result.status, agent.NotificationStatus.SENT)
        acknowledged.assert_called_once_with(item["fingerprint"])
        self.assertEqual(self.outbox.get(item["notification_id"])["state"], DELIVERED)

    def test_telegram_success_persists_outbox_and_compatibility_fingerprint(self):
        item = self.enqueue()
        fingerprint_path = Path(self.temporary.name) / "last_notification.json"
        bot = Mock(send_message=AsyncMock(return_value=None))
        with patch.object(agent, "TRADE_NOTIFICATION_OUTBOX", self.outbox), \
                patch.object(agent, "BOT_TOKEN", "token"), \
                patch.object(agent, "load_chat_id", return_value=1), \
                patch.object(agent, "Bot", return_value=bot), \
                patch.object(notifications, "LAST_FILE", fingerprint_path):
            result = asyncio.run(agent.send_outbox_notification(item["notification_id"]))
        self.assertEqual(result.status, agent.NotificationStatus.SENT)
        self.assertEqual(self.outbox.get(item["notification_id"])["state"], DELIVERED)
        fingerprint_state = json.loads(fingerprint_path.read_text(encoding="utf-8"))
        self.assertIn(item["fingerprint"], fingerprint_state["fingerprints"])

    def test_delivered_item_is_never_resent(self):
        item = self.enqueue()
        self.outbox.claim(item["notification_id"])
        self.outbox.mark_delivered(item["notification_id"])
        with patch.object(agent, "TRADE_NOTIFICATION_OUTBOX", self.outbox), \
                patch.object(agent, "Bot") as bot:
            result = asyncio.run(agent.send_outbox_notification(item["notification_id"]))
        self.assertEqual(result.reason, "in_progress_or_not_due")
        bot.assert_not_called()

    def test_existing_signal_cooldown_permanently_skips_new_trade_item(self):
        item = self.enqueue()
        with patch.object(agent, "TRADE_NOTIFICATION_OUTBOX", self.outbox), \
                patch.object(agent, "BOT_TOKEN", "token"), \
                patch.object(agent, "load_chat_id", return_value=1), \
                patch.object(agent, "is_duplicate", return_value=True), \
                patch.object(agent, "Bot") as bot:
            result = asyncio.run(agent.send_outbox_notification(item["notification_id"]))
        self.assertEqual(result.status, agent.NotificationStatus.SKIPPED)
        self.assertEqual(result.reason, "duplicate")
        self.assertEqual(self.outbox.get(item["notification_id"])["state"], PERMANENT_SKIP)
        bot.assert_not_called()

    def test_outbox_delivery_history_preserves_cooldown_if_compatibility_write_fails(self):
        first = self.enqueue("LIVE-1")
        bot = Mock(send_message=AsyncMock(return_value=None))
        with patch.object(agent, "TRADE_NOTIFICATION_OUTBOX", self.outbox), \
                patch.object(agent, "BOT_TOKEN", "token"), \
                patch.object(agent, "load_chat_id", return_value=1), \
                patch.object(agent, "Bot", return_value=bot), \
                patch.object(agent, "is_duplicate", return_value=False), \
                patch.object(agent, "mark_as_sent", side_effect=OSError("state disk full")):
            result = asyncio.run(agent.send_outbox_notification(first["notification_id"]))
        self.assertEqual(result.status, agent.NotificationStatus.SENT)
        self.assertEqual(result.reason, "sent_compatibility_state_error")
        self.assertEqual(self.outbox.get(first["notification_id"])["state"], DELIVERED)

        second_trade = self.trade("LIVE-2")
        second_trade["research_metadata_json"] = self.trade("LIVE-1")["research_metadata_json"]
        second = self.outbox.enqueue_from_trade(second_trade)
        second_bot = Mock(send_message=AsyncMock(return_value=None))
        with patch.object(agent, "TRADE_NOTIFICATION_OUTBOX", self.outbox), \
                patch.object(agent, "BOT_TOKEN", "token"), \
                patch.object(agent, "load_chat_id", return_value=1), \
                patch.object(agent, "is_duplicate", return_value=False), \
                patch.object(agent, "Bot", return_value=second_bot):
            suppressed = asyncio.run(agent.send_outbox_notification(second["notification_id"]))
        self.assertEqual(suppressed.reason, "duplicate")
        second_bot.assert_not_called()

    def test_fingerprint_acknowledgement_occurs_only_after_send(self):
        item = self.enqueue()
        events = []
        bot = Mock(send_message=AsyncMock(side_effect=lambda **_kwargs: events.append("telegram")))
        with patch.object(agent, "TRADE_NOTIFICATION_OUTBOX", self.outbox), \
                patch.object(agent, "BOT_TOKEN", "token"), \
                patch.object(agent, "load_chat_id", return_value=1), \
                patch.object(agent, "Bot", return_value=bot), \
                patch.object(agent, "is_duplicate", return_value=False), \
                patch.object(agent, "mark_as_sent", side_effect=lambda _fp: events.append("fingerprint")):
            result = asyncio.run(agent.send_outbox_notification(item["notification_id"]))
        self.assertEqual(result.status, agent.NotificationStatus.SENT)
        self.assertEqual(events, ["telegram", "fingerprint"])

    def test_crash_after_send_before_durable_receipt_may_duplicate_on_retry(self):
        item = self.enqueue()
        bot = Mock(send_message=AsyncMock(return_value=None))
        with patch.object(agent, "TRADE_NOTIFICATION_OUTBOX", self.outbox), \
                patch.object(agent, "BOT_TOKEN", "token"), \
                patch.object(agent, "load_chat_id", return_value=1), \
                patch.object(agent, "Bot", return_value=bot), \
                patch.object(agent, "is_duplicate", return_value=False), \
                patch.object(agent, "mark_as_sent") as acknowledged, \
                patch.object(self.outbox, "mark_delivered", side_effect=OutboxPersistError("crash")):
            result = asyncio.run(agent.send_outbox_notification(item["notification_id"]))
        self.assertEqual(result.reason, "delivered_state_persist_error")
        acknowledged.assert_not_called()
        self.assertEqual(self.outbox.get(item["notification_id"])["state"], SENDING)

        self.outbox.recover_interrupted()
        second_bot = Mock(send_message=AsyncMock(return_value=None))
        with patch.object(agent, "TRADE_NOTIFICATION_OUTBOX", self.outbox), \
                patch.object(agent, "BOT_TOKEN", "token"), \
                patch.object(agent, "load_chat_id", return_value=1), \
                patch.object(agent, "Bot", return_value=second_bot), \
                patch.object(agent, "is_duplicate", return_value=False), \
                patch.object(agent, "mark_as_sent"):
            recovered = asyncio.run(agent.send_outbox_notification(item["notification_id"]))
        self.assertEqual(recovered.status, agent.NotificationStatus.SENT)
        second_bot.send_message.assert_awaited_once()
        self.assertEqual(self.outbox.get(item["notification_id"])["state"], DELIVERED)

    def test_corrupt_outbox_fails_closed_without_overwrite(self):
        self.path.write_text("{broken", encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(OutboxCorruptError):
            self.outbox.enqueue_from_trade(self.trade())
        self.assertEqual(self.path.read_bytes(), before)

    def test_notification_identity_must_derive_from_trade_identity(self):
        item = self.enqueue()
        state = json.loads(self.path.read_text(encoding="utf-8"))
        state["items"][item["notification_id"]]["trade_id"] = "LIVE-tampered"
        self.path.write_text(json.dumps(state), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(OutboxCorruptError):
            self.outbox.get(item["notification_id"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_semantic_corruption_isolated_from_trading_cycle(self):
        item = self.enqueue()
        self.outbox.claim(item["notification_id"])
        self.outbox.mark_delivered(item["notification_id"])
        state = json.loads(self.path.read_text(encoding="utf-8"))
        state["items"][item["notification_id"]]["delivered_at"] = "not-a-timestamp"
        self.path.write_text(json.dumps(state), encoding="utf-8")
        before = self.path.read_bytes()
        with patch.object(agent, "TRADE_NOTIFICATION_OUTBOX", self.outbox), \
                patch.object(agent, "get_all_trades", return_value=[]), \
                patch.object(agent.LOGGER, "timestamped") as logged:
            agent.process_notification_outbox()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(logged.called)
        self.assertIn("trade_notification_outbox_error", logged.call_args.args[0])

    def test_non_finite_retry_timestamps_fail_closed(self):
        item = self.enqueue()
        valid = self.path.read_bytes()
        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid=invalid):
                state = json.loads(valid)
                state["items"][item["notification_id"]]["next_attempt_at"] = invalid
                self.path.write_text(json.dumps(state), encoding="utf-8")
                before = self.path.read_bytes()
                with self.assertRaises(OutboxCorruptError):
                    self.outbox.due_ids()
                self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_sorting_fields_fail_closed_without_blocking_cycle(self):
        first = self.enqueue("LIVE-1")
        self.enqueue("LIVE-2")
        valid = self.path.read_bytes()
        for field, invalid in (("created_at", {}), ("opened_at", []), ("history", {})):
            with self.subTest(field=field):
                state = json.loads(valid)
                state["items"][first["notification_id"]][field] = invalid
                self.path.write_text(json.dumps(state), encoding="utf-8")
                before = self.path.read_bytes()
                with patch.object(agent, "TRADE_NOTIFICATION_OUTBOX", self.outbox), \
                        patch.object(agent.LOGGER, "timestamped") as logged:
                    agent.process_notification_outbox()
                self.assertEqual(self.path.read_bytes(), before)
                self.assertIn("trade_notification_outbox_error", logged.call_args.args[0])

    def test_atomic_write_failure_preserves_previous_valid_state(self):
        self.enqueue()
        before = self.path.read_bytes()
        with patch.object(outbox_module.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OutboxPersistError):
                self.outbox.enqueue_from_trade(self.trade("LIVE-2"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_reentrant_claim_cannot_double_attempt(self):
        item = self.enqueue()
        first = self.outbox.claim(item["notification_id"])
        second = TradeNotificationOutbox(self.path, now=lambda: self.clock[0]).claim(
            item["notification_id"]
        )
        self.assertEqual(first["state"], SENDING)
        self.assertIsNone(second)
        self.assertEqual(self.outbox.get(item["notification_id"])["attempt_count"], 1)

    def test_missing_configuration_is_explicit_and_not_delivered(self):
        item = self.enqueue()
        with patch.object(agent, "TRADE_NOTIFICATION_OUTBOX", self.outbox), \
                patch.object(agent, "BOT_TOKEN", ""), \
                patch.object(agent, "load_chat_id", return_value=None), \
                patch.object(agent, "Bot") as bot:
            result = asyncio.run(agent.send_outbox_notification(item["notification_id"]))
        self.assertEqual(result.status, agent.NotificationStatus.SKIPPED)
        self.assertEqual(result.reason, "missing_configuration")
        stored = self.outbox.get(item["notification_id"])
        self.assertEqual(stored["state"], RETRYABLE_ERROR)
        self.assertEqual(stored["last_result"], "SKIPPED")
        bot.assert_not_called()

    def test_due_processing_is_bounded_and_backoff_is_capped(self):
        ids = [self.enqueue(f"LIVE-{number}")["notification_id"] for number in range(5)]
        self.assertEqual(len(self.outbox.due_ids(limit=3)), 3)
        item = self.outbox.claim(ids[0])
        delays = []
        for attempt in range(8):
            self.outbox.mark_retryable(ids[0], f"failure-{attempt}")
            stored = self.outbox.get(ids[0])
            delays.append(stored["next_attempt_at"] - self.clock[0])
            self.clock[0] = stored["next_attempt_at"]
            if attempt < 7:
                self.outbox.claim(ids[0])
        self.assertEqual(delays[:3], [10, 20, 40])
        self.assertTrue(all(delay <= 40 for delay in delays))

    def test_symlink_state_is_rejected(self):
        target = Path(self.temporary.name) / "target.json"
        target.write_text(json.dumps({"schema_version": 1, "items": {}}), encoding="utf-8")
        os.symlink(target, self.path)
        with self.assertRaises(OutboxCorruptError):
            self.outbox.get("anything")
