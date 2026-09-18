#!/usr/bin/env python3
"""Tests for Feishu CLI-backed Base event synchronization."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from feishu_sync import (
    DASHBOARD_BLOCKS,
    DEVICE_DASHBOARD_BLOCKS,
    EVENT_TABLE_FIELDS,
    PLAY_TABLE_FIELDS,
    SESSION_TABLE_FIELDS,
    FeishuBaseClient,
    FeishuSyncWorker,
    _batch_token,
    event_fields,
    playback_fields,
)
from usage_analytics import AnalyticsEvent, AnalyticsStore


class FakeCli:
    def __init__(self, responses: list[dict] | None = None):
        self.responses = list(responses or [])
        self.calls: list[tuple[list[str], float]] = []

    def run(self, arguments, *, timeout=60):
        self.calls.append((list(arguments), timeout))
        return self.responses.pop(0) if self.responses else {"ok": True, "data": {}}


class FeishuSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = AnalyticsStore(Path(self.temporary_directory.name) / "analytics.sqlite3")
        self.cli = FakeCli()
        self.client = FeishuBaseClient(
            self.cli, "base-token", "tbl-events", "tbl-sessions", "tbl-plays"
        )  # type: ignore[arg-type]

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_event_mapping_and_batch_token_are_stable(self) -> None:
        event = AnalyticsEvent.create("music_search_started", source="mcp", event_id="event-1", payload={"query": "海阔天空"})
        fields = event_fields(event)
        self.assertEqual(fields["事件ID"], "event-1")
        self.assertIn("海阔天空", fields["事件摘要"])
        self.assertEqual(_batch_token([event]), _batch_token([event]))

    def test_cache_fields_are_mapped_to_feishu(self) -> None:
        event = AnalyticsEvent.create(
            "music_cache_saved",
            source="proxy",
            payload={
                "cache_hit": True,
                "delivery_source": "cache",
                "cache_id": "cache-1",
                "size_bytes": 1234,
            },
        )
        fields = event_fields(event)
        self.assertTrue(fields["缓存命中"])
        self.assertEqual(fields["传输来源"], "cache")
        self.assertEqual(fields["缓存ID"], "cache-1")
        self.assertEqual(fields["缓存字节"], 1234)
        playback = playback_fields(
            {
                "playback_id": "play-cache",
                "provider": "netease",
                "cache_hit": 1,
                "delivery_source": "cache",
                "cache_id": "cache-1",
            }
        )
        self.assertTrue(playback["缓存播放"])
        self.assertEqual(playback["Provider"], "netease")
        self.assertEqual(playback["传输来源"], "cache")

    def test_sync_uses_cli_api_with_idempotency_token(self) -> None:
        events = [
            AnalyticsEvent.create("music_search_started", source="mcp", event_id=f"event-{index}")
            for index in range(2)
        ]
        for event in events:
            self.store.append(event)

        count = FeishuSyncWorker(self.store, self.client).sync_once()

        self.assertEqual(count, 2)
        self.assertEqual(self.store.status()["synced"], 2)
        arguments = self.cli.calls[0][0]
        self.assertEqual(arguments[:2], ["api", "POST"])
        params = json.loads(arguments[arguments.index("--params") + 1])
        records = json.loads(arguments[arguments.index("--data") + 1])["records"]
        self.assertRegex(params["client_token"], r"^[0-9a-f-]{36}$")
        self.assertEqual(len(records), 2)

    def test_sync_creates_then_updates_playback_projection(self) -> None:
        cli = FakeCli(
            [
                {"ok": True, "data": {}},
                {"ok": True, "data": {"data": [], "fields": ["播放ID"], "record_id_list": []}},
                {"ok": True, "data": {"record": {"create": {"播放ID": "play-1"}}, "created": True}},
                {
                    "ok": True,
                    "data": {
                        "data": [["play-1"]],
                        "fields": ["播放ID"],
                        "record_id_list": ["rec-play"],
                    },
                },
            ]
        )
        client = FeishuBaseClient(
            cli, "base-token", "tbl-events", "tbl-sessions", "tbl-plays"
        )  # type: ignore[arg-type]
        event = AnalyticsEvent.create(
            "playback_completed",
            source="firmware",
            event_id="play-end",
            payload={
                "playback_id": "play-1",
                "title": "海阔天空",
                "audible_played_ms": 120000,
                "media_duration_ms": 120000,
            },
        )
        self.store.append(event)

        self.assertEqual(FeishuSyncWorker(self.store, client).sync_once(), 2)
        upsert = next(call[0] for call in cli.calls if call[0][1] == "+record-upsert")
        self.assertEqual(upsert[:2], ["base", "+record-upsert"])
        self.assertNotIn("--record-id", upsert)
        fields = json.loads(upsert[upsert.index("--json") + 1])
        self.assertEqual(fields["播放ID"], "play-1")
        self.assertTrue(fields["自然完播"])

        changed = AnalyticsEvent.create(
            "audio_underrun",
            source="firmware",
            event_id="play-underrun",
            payload={"playback_id": "play-1", "wait_ms": 80},
        )
        self.store.append(changed)
        cli.responses.extend(
            [
                {"ok": True, "data": {}},
                {"ok": True, "data": {"record_id_list": ["rec-play"]}},
            ]
        )
        self.assertEqual(FeishuSyncWorker(self.store, client).sync_once(), 2)
        update = cli.calls[-1][0]
        self.assertEqual(update[update.index("--record-id") + 1], "rec-play")

    def test_long_session_id_uses_prefix_search_and_matrix_record_id(self) -> None:
        session_id = "ad48f209-e377-48b8-bb8f-e9f1e47606ae:session:20446171"
        cli = FakeCli(
            [
                {"ok": True, "data": {}},
                {
                    "ok": True,
                    "data": {
                        "data": [[session_id]],
                        "fields": ["会话ID"],
                        "record_id_list": ["rec-session"],
                    },
                },
                {"ok": True, "data": {"record_id_list": ["rec-session"]}},
            ]
        )
        client = FeishuBaseClient(
            cli, "base-token", "tbl-events", "tbl-sessions", "tbl-plays"
        )  # type: ignore[arg-type]
        self.store.append(
            AnalyticsEvent.create(
                "wake_detected",
                source="firmware",
                event_id="wake-long",
                session_id=session_id,
                payload={"wake_method": "wake_word", "monotonic_ms": 100},
            )
        )

        self.assertEqual(FeishuSyncWorker(self.store, client).sync_once(), 2)
        search = cli.calls[1][0]
        search_json = json.loads(search[search.index("--json") + 1])
        self.assertEqual(search_json["keyword"], session_id[:50])
        self.assertEqual(len(search_json["keyword"]), 50)
        update = cli.calls[2][0]
        self.assertEqual(update[update.index("--record-id") + 1], "rec-session")

    def test_initialize_reuses_existing_schema_and_dashboard(self) -> None:
        cli = FakeCli(
            [
                {"ok": True, "data": {"tables": [{"name": "原始事件", "id": "tbl-existing"}]}},
                {"ok": True, "data": {"fields": [{"name": field["name"]} for field in EVENT_TABLE_FIELDS]}},
                {"ok": True, "data": {"table_id": "tbl-sessions"}},
                {"ok": True, "data": {"table_id": "tbl-plays"}},
                {"ok": True, "data": {"dashboards": [{"name": "小智使用分析", "id": "dbs-existing"}]}},
                {"ok": True, "data": {"items": [{"name": block[1]} for block in DASHBOARD_BLOCKS]}},
                {"ok": True, "data": {"dashboards": [{"name": "小智设备稳定性", "id": "dbs-device"}]}},
                {"ok": True, "data": {"items": [{"name": block[1]} for block in DEVICE_DASHBOARD_BLOCKS]}},
            ]
        )
        client = FeishuBaseClient(cli, "base-token")  # type: ignore[arg-type]

        table_id, session_table_id, play_table_id, dashboard_id = client.initialize()

        self.assertEqual(
            (table_id, session_table_id, play_table_id, dashboard_id),
            ("tbl-existing", "tbl-sessions", "tbl-plays", "dbs-existing"),
        )
        self.assertEqual(len(cli.calls), 8)

    def test_create_base_extracts_token_default_table_and_url(self) -> None:
        cli = FakeCli(
            [
                {
                    "ok": True,
                    "data": {
                        "app": {
                            "app_token": "base-created",
                            "default_table_id": "tbl-default",
                            "url": "https://example.feishu.cn/base/base-created",
                        }
                    },
                }
            ]
        )

        client, table_id, url = FeishuBaseClient.create_base("小智使用分析", cli=cli)  # type: ignore[arg-type]

        self.assertEqual(client.base_token, "base-created")
        self.assertEqual(table_id, "tbl-default")
        self.assertEqual(url, "https://example.feishu.cn/base/base-created")
        arguments = cli.calls[0][0]
        self.assertEqual(arguments[:2], ["base", "+base-create"])
        self.assertIn("--as", arguments)
        self.assertEqual(arguments[arguments.index("--as") + 1], "user")

    def test_fresh_base_reuses_and_renames_default_table(self) -> None:
        cli = FakeCli(
            [
                {"ok": True, "data": {"tables": [{"name": "数据表", "id": "tbl-default"}]}},
                {"ok": True, "data": {}},
                {"ok": True, "data": {"fields": [{"id": "fld-primary", "name": "文本"}]}},
                {"ok": True, "data": {}},
                *({"ok": True, "data": {}} for _ in EVENT_TABLE_FIELDS[1:]),
                {"ok": True, "data": {"table_id": "tbl-sessions"}},
                {"ok": True, "data": {"table_id": "tbl-plays"}},
                {"ok": True, "data": {"dashboards": [{"name": "小智使用分析", "id": "dbs-existing"}]}},
                {"ok": True, "data": {"items": [{"name": block[1]} for block in DASHBOARD_BLOCKS]}},
                {"ok": True, "data": {"dashboards": [{"name": "小智设备稳定性", "id": "dbs-device"}]}},
                {"ok": True, "data": {"items": [{"name": block[1]} for block in DEVICE_DASHBOARD_BLOCKS]}},
            ]
        )
        client = FeishuBaseClient(cli, "base-created")  # type: ignore[arg-type]

        table_id, session_table_id, play_table_id, dashboard_id = client.initialize(
            fresh_base=True, default_table_id="tbl-default"
        )

        self.assertEqual(
            (table_id, session_table_id, play_table_id, dashboard_id),
            ("tbl-default", "tbl-sessions", "tbl-plays", "dbs-existing"),
        )
        commands = [call[0][1] for call in cli.calls]
        self.assertIn("+table-update", commands)
        self.assertIn("+field-update", commands)
        field_update = next(call[0] for call in cli.calls if call[0][1] == "+field-update")
        self.assertIn("--yes", field_update)
        self.assertEqual(commands.count("+table-create"), 2)

if __name__ == "__main__":
    unittest.main()
