#!/usr/bin/env python3
"""Tests for local-first usage analytics storage and privacy controls."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

    def test_playback_projection_keeps_natural_completion_separate_from_preview(self) -> None:
        recorder = AnalyticsRecorder(self.store)
        for event_id, event_type, clock, payload in (
            (
                "play-1:end",
                "playback_completed",
                121000,
                {"audible_played_ms": 120000, "elapsed_since_start_ms": 120000},
            ),
            ("play-1:start", "playback_started", 1000, {"audible_played_ms": 0}),
        ):
            event = recorder.create_event(
                event_type,
                source="firmware",
                event_id=event_id,
                trace_id="trace-1",
                payload={
                    "playback_id": "play-1",
                    "monotonic_ms": clock,
                    "title": "试听歌曲",
                    "playback_access": "preview",
                    "media_duration_ms": 120000,
                    "song_duration_ms": 240000,
                    **payload,
                },
            )
            self.store.append(event)

        playback = self.store.get_playback("play-1")
        assert playback is not None
        self.assertEqual(playback["status"], "completed")
        self.assertEqual(playback["end_reason"], "natural_completed")
        self.assertEqual(playback["natural_completed"], 1)
        self.assertEqual(playback["playback_access"], "preview")
        self.assertEqual(playback["play_ratio"], 1.0)
        self.assertTrue(playback["started_at"])

    def test_playback_projection_calculates_first_audio_wait_from_search(self) -> None:
        recorder = AnalyticsRecorder(self.store)
        requested = datetime(2026, 9, 13, 1, 2, 3, tzinfo=timezone.utc)
        recorder.emit(
            "music_search_succeeded", source="mcp", trace_id="trace-wait",
            event_id="search-wait", occurred_at=requested,
            payload={"query": "海阔天空"},
        )
        recorder.emit(
            "playback_started", source="firmware", trace_id="trace-wait",
            event_id="play-wait", occurred_at=requested + timedelta(milliseconds=1750),
            payload={"playback_id": "play-wait", "monotonic_ms": 1000},
        )

        playback = self.store.get_playback("play-wait")
        assert playback is not None
        self.assertEqual(playback["first_audio_wait_ms"], 1750)

    def test_rebuild_playback_projections_is_idempotent(self) -> None:
        recorder = AnalyticsRecorder(self.store)
        recorder.emit(
            "playback_stopped",
            source="firmware",
            event_id="stop-1",
            payload={
                "playback_id": "play-1",
                "monotonic_ms": 9000,
                "audible_played_ms": 8000,
                "media_duration_ms": 100000,
                "end_reason": "song_switched",
            },
        )
        self.assertEqual(self.store.rebuild_playback_projections(), 1)
        first = self.store.get_playback("play-1")
        self.assertEqual(self.store.rebuild_playback_projections(), 1)
        second = self.store.get_playback("play-1")
        assert first is not None and second is not None
        self.assertEqual(first["end_reason"], second["end_reason"])
        self.assertEqual(first["audible_played_ms"], second["audible_played_ms"])

    def test_session_projection_and_projection_outbox(self) -> None:
        recorder = AnalyticsRecorder(self.store, privacy_mode="full")
        recorder.emit(
            "wake_detected",
            source="firmware",
            event_id="session-1:wake",
            session_id="session-1",
            device_id="raw-device",
            payload={"wake_method": "wake_word", "firmware_version": "1.2.3", "monotonic_ms": 100},
        )
        recorder.emit(
            "user_utterance",
            source="firmware",
            event_id="session-1:user",
            session_id="session-1",
            payload={"user_text": "播放海阔天空", "monotonic_ms": 200},
        )

        session = self.store.get_session("session-1")
        assert session is not None
        self.assertEqual(session["wake_method"], "wake_word")
        self.assertEqual(session["user_text"], "播放海阔天空")
        self.assertEqual(session["turn_count"], 1)
        self.assertNotEqual(session["device_id"], "raw-device")

        items = self.store.claim_projection_batch()
        self.assertEqual([(item.entity_type, item.entity_id) for item in items], [("session", "session-1")])
        self.store.mark_projection_synced("session", "session-1", items[0].revision, "rec-session")
        self.assertEqual(self.store.playback_status()["synced"], 1)

    def test_quick_skip_followed_by_search_marks_dissatisfaction(self) -> None:
        recorder = AnalyticsRecorder(self.store)
        recorder.emit(
            "music_search_succeeded",
            source="mcp",
            event_id="search-success",
            trace_id="trace-1",
            payload={"query": "世界真细小", "normalized_query": "世界真细小"},
        )
        recorder.emit(
            "playback_stopped",
            source="firmware",
            event_id="play-stop",
            trace_id="trace-1",
            payload={
                "playback_id": "play-1",
                "monotonic_ms": 9000,
                "audible_played_ms": 8000,
                "elapsed_since_start_ms": 8000,
                "media_duration_ms": 180000,
                "end_reason": "user_stopped",
            },
        )
        recorder.emit(
            "music_search_started",
            source="mcp",
            event_id="search-again",
            payload={"query": "世界真细小"},
        )

        playback = self.store.get_playback("play-1")
        assert playback is not None
        self.assertEqual(playback["original_query"], "世界真细小")
        self.assertEqual(playback["suspected_search_dissatisfaction"], 1)
        self.assertEqual(playback["dissatisfaction_reason"], "same_query_retried")
        self.assertEqual(playback["next_action"], "searched_again")

    def test_session_projection_accumulates_turns_and_ignores_stale_text(self) -> None:
        recorder = AnalyticsRecorder(self.store, privacy_mode="full")
        for event_id, event_type, clock, payload in (
            ("wake", "wake_detected", 100, {"wake_method": "wake_word"}),
            ("user-1", "user_utterance", 200, {"user_text": "播放海阔天空"}),
            ("assistant-1", "assistant_response", 300, {"assistant_text": "好的"}),
            ("assistant-2", "assistant_response", 310, {"assistant_text": "马上播放"}),
            ("user-2", "user_utterance", 400, {"user_text": "声音大一点"}),
            ("stale", "assistant_response", 250, {"assistant_text": "过期响应"}),
        ):
            recorder.emit(
                event_type,
                source="firmware",
                event_id=event_id,
                session_id="session-multi",
                payload={**payload, "monotonic_ms": clock},
            )

        session = self.store.get_session("session-multi")
        assert session is not None
        self.assertEqual(session["user_text"], "播放海阔天空\n声音大一点")
        self.assertEqual(session["assistant_text"], "好的\n马上播放")
        self.assertEqual(session["turn_count"], 2)

    def test_session_projection_records_each_terminal_playback_result(self) -> None:
        recorder = AnalyticsRecorder(self.store, privacy_mode="full")
        expected = {
            "playback_completed": "completed",
            "playback_stopped": "stopped",
            "song_switched": "switched",
            "playback_failed": "failed",
        }
        for index, (event_type, result) in enumerate(expected.items()):
            session_id = f"session-result-{index}"
            recorder.emit(
                event_type,
                source="firmware",
                event_id=f"event-result-{index}",
                session_id=session_id,
                payload={
                    "playback_id": f"play-result-{index}",
                    "monotonic_ms": 1000 + index,
                    "end_reason": "user_stopped" if event_type == "playback_stopped" else "",
                },
            )
            session = self.store.get_session(session_id)
            assert session is not None
            self.assertEqual(session["result"], result)

    def test_query_delete_and_retention_preserve_unsynced_rows(self) -> None:
        old_time = datetime.now(timezone.utc) - timedelta(days=200)
        recorder = AnalyticsRecorder(self.store, privacy_mode="full")
        old_synced = AnalyticsEvent.create(
            "wake_detected",
            source="firmware",
            event_id="old-synced",
            session_id="old-session",
            payload={"monotonic_ms": 1},
            occurred_at=old_time,
            privacy_mode="full",
        )
        old_pending = AnalyticsEvent.create(
            "wake_detected",
            source="firmware",
            event_id="old-pending",
            session_id="pending-session",
            payload={"monotonic_ms": 1},
            occurred_at=old_time,
            privacy_mode="full",
        )
        self.store.append(old_synced)
        self.store.append(old_pending)
        self.store.mark_synced("old-synced")
        projection_items = self.store.claim_projection_batch()
        projection = next(item for item in projection_items if item.entity_id == "old-session")
        self.store.mark_projection_synced(
            projection.entity_type, projection.entity_id, projection.revision, "rec-old"
        )
        self.store.release_projection_batch(
            [item for item in projection_items if item.entity_id == "pending-session"],
            "pending", delay_seconds=0,
        )

        deleted = self.store.cleanup_retention(raw_days=30, projection_days=180)
        self.assertEqual(deleted, {"events": 1, "playbacks": 0, "sessions": 1})
        self.assertIsNone(self.store.get_session("old-session"))
        self.assertIsNotNone(self.store.get_session("pending-session"))
        self.assertEqual(len(self.store.query_sessions(limit=10)), 1)

        result = self.store.delete_session("pending-session")
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["sessions"], 1)
        self.assertEqual(self.store.query_sessions(limit=10), [])


if __name__ == "__main__":
    unittest.main()
