#!/usr/bin/env python3
"""Tests for Feishu CLI-backed Base event synchronization."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from feishu_sync import (
    DASHBOARD_BLOCKS,
    EVENT_TABLE_FIELDS,
    FeishuBaseClient,
    FeishuSyncWorker,
    _batch_token,
    event_fields,
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
        self.client = FeishuBaseClient(self.cli, "base-token", "tbl-events")  # type: ignore[arg-type]

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_event_mapping_and_batch_token_are_stable(self) -> None:
        event = AnalyticsEvent.create("music_search_started", source="mcp", event_id="event-1", payload={"query": "海阔天空"})
        fields = event_fields(event)
        self.assertEqual(fields["事件ID"], "event-1")
        self.assertIn("海阔天空", fields["事件摘要"])
        self.assertEqual(_batch_token([event]), _batch_token([event]))

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

    def test_initialize_reuses_existing_schema_and_dashboard(self) -> None:
        cli = FakeCli(
            [
                {"ok": True, "data": {"tables": [{"name": "原始事件", "id": "tbl-existing"}]}},
                {"ok": True, "data": {"fields": [{"name": field["name"]} for field in EVENT_TABLE_FIELDS]}},
                {"ok": True, "data": {"dashboards": [{"name": "小智使用分析", "id": "dbs-existing"}]}},
                {"ok": True, "data": {"items": [{"name": block[1]} for block in DASHBOARD_BLOCKS]}},
            ]
        )
        client = FeishuBaseClient(cli, "base-token")  # type: ignore[arg-type]

        table_id, dashboard_id = client.initialize()

        self.assertEqual((table_id, dashboard_id), ("tbl-existing", "dbs-existing"))
        self.assertEqual(len(cli.calls), 4)


if __name__ == "__main__":
    unittest.main()
