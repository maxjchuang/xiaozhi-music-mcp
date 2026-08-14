#!/usr/bin/env python3
"""Tests for local-first usage analytics storage and privacy controls."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from usage_analytics import AnalyticsEvent, AnalyticsRecorder, AnalyticsStore, mask_text, sanitize_payload


class UsageAnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "analytics.sqlite3"
        self.store = AnalyticsStore(self.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_append_is_idempotent_and_creates_outbox_item(self) -> None:
        event = AnalyticsEvent.create(
            "music_search_started",
            source="mcp",
            event_id="fixed-event",
            trace_id="trace-1",
            payload={"query": "海阔天空"},
        )

        self.assertTrue(self.store.append(event))
        self.assertFalse(self.store.append(event))
        self.assertEqual(self.store.status(), {"pending": 1, "sending": 0, "synced": 0, "dead": 0, "events": 1})

        claimed = self.store.claim_batch()
        self.assertEqual([item.event.event_id for item in claimed], ["fixed-event"])
        self.assertEqual(claimed[0].event.payload, {"query": "海阔天空"})

        self.store.mark_synced("fixed-event", "rec123")
        self.assertEqual(self.store.status()["synced"], 1)
        self.assertEqual(self.store.event_sync_status("fixed-event"), "synced")

    def test_failure_retries_then_moves_to_dead_letter(self) -> None:
        event = AnalyticsEvent.create("network_error", source="provider", event_id="failed-event")
        self.store.append(event)
        self.store.claim_batch()

        self.assertEqual(self.store.mark_failed("failed-event", "token=secret", max_attempts=2), "pending")
        # A normal retry is delayed; explicitly reset through the dead-letter path on the next claim.
        with self.store._connection() as connection:
            connection.execute(
                "UPDATE sync_outbox SET status = 'sending', next_attempt_at = 0 WHERE event_id = ?",
                ("failed-event",),
            )
        self.assertEqual(self.store.mark_failed("failed-event", "again", max_attempts=2), "dead")
        self.assertEqual(self.store.status()["dead"], 1)
        self.assertEqual(self.store.retry_dead(), 1)
        self.assertEqual(self.store.status()["pending"], 1)

    def test_privacy_modes_and_secret_redaction(self) -> None:
        payload = {
            "user_text": "我的手机号是 13812345678，邮箱 max@example.com",
            "query": "token=abcdef 海阔天空",
        }
        masked = sanitize_payload(payload, "masked")
        self.assertNotIn("13812345678", masked["user_text"])
        self.assertNotIn("max@example.com", masked["user_text"])
        self.assertNotIn("abcdef", masked["query"])
        self.assertNotIn("user_text", sanitize_payload(payload, "off"))
        self.assertEqual(sanitize_payload(payload, "full")["user_text"], payload["user_text"])
        self.assertEqual(mask_text("Authorization: Bearer abc.def"), "Authorization: Bearer ***")

    def test_recorder_never_raises_when_storage_fails(self) -> None:
        recorder = AnalyticsRecorder(self.store)
        self.store.append = lambda event: (_ for _ in ()).throw(OSError("disk full"))  # type: ignore[method-assign]
        event_id = recorder.emit("music_search_failed", source="mcp")
        self.assertTrue(event_id)

    def test_release_batch_does_not_consume_retry_budget(self) -> None:
        event = AnalyticsEvent.create("music_search_started", source="mcp", event_id="auth-wait")
        self.store.append(event)
        self.store.claim_batch()
        self.store.release_batch([event.event_id], "AUTH_REQUIRED", delay_seconds=0)
        claimed = self.store.claim_batch()
        self.assertEqual(claimed[0].attempts, 0)


if __name__ == "__main__":
    unittest.main()
