#!/usr/bin/env python3
"""Synchronize the local analytics outbox through the authenticated Feishu CLI."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import logging
import os
import threading
from typing import Any, Mapping
import uuid
from zoneinfo import ZoneInfo

from lark_cli import LarkCli, LarkCliError
from usage_analytics import AnalyticsEvent, AnalyticsStore, default_database_path, mask_text


LOGGER = logging.getLogger("xiaozhi-feishu-sync")


class FeishuApiError(RuntimeError):
    pass


def _timestamp_milliseconds(value: str) -> int:
    return round(datetime.fromisoformat(value).timestamp() * 1000)


def _batch_token(events: list[AnalyticsEvent]) -> str:
    digest = hashlib.sha256("\n".join(event.event_id for event in events).encode("utf-8")).digest()[:16]
    return str(uuid.UUID(bytes=digest, version=4))


def event_fields(event: AnalyticsEvent) -> dict[str, Any]:
    payload = event.payload or {}
    summary = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    local_hour = datetime.fromisoformat(event.occurred_at).astimezone(ZoneInfo("Asia/Shanghai")).hour
    if local_hour < 6:
        period = "凌晨"
    elif local_hour < 12:
        period = "上午"
    elif local_hour < 14:
        period = "中午"
    elif local_hour < 18:
        period = "下午"
    else:
        period = "晚上"
    result = "成功" if event.event_type.endswith(("succeeded", "completed")) or event.event_type == "playback_started" else (
        "失败" if event.event_type.endswith(("failed", "error")) else ""
    )
    fields = {
        "事件ID": event.event_id,
        "事件类型": event.event_type,
        "发生时间": _timestamp_milliseconds(event.occurred_at),
        "接收时间": _timestamp_milliseconds(event.received_at),
        "来源": event.source,
        "设备ID": event.device_id,
        "会话ID": event.session_id,
        "链路ID": event.trace_id,
        "事件摘要": summary[:10000],
        "结构版本": event.schema_version,
        "搜索词": str(payload.get("query", ""))[:500],
        "歌曲名": str(payload.get("title", ""))[:500],
        "歌手": str(payload.get("artist", ""))[:500],
        "Provider": str(payload.get("provider", ""))[:200],
        "结果": result,
        "耗时毫秒": payload.get("elapsed_ms") if isinstance(payload.get("elapsed_ms"), (int, float)) else None,
        "使用时段": period,
    }
    return {key: value for key, value in fields.items() if value is not None}


EVENT_TABLE_FIELDS = (
    {"name": "事件ID", "type": "text"},
    {"name": "事件类型", "type": "text"},
    {"name": "发生时间", "type": "datetime", "style": {"format": "yyyy-MM-dd HH:mm"}},
    {"name": "接收时间", "type": "datetime", "style": {"format": "yyyy-MM-dd HH:mm"}},
    {"name": "来源", "type": "text"},
    {"name": "设备ID", "type": "text"},
    {"name": "会话ID", "type": "text"},
    {"name": "链路ID", "type": "text"},
    {"name": "事件摘要", "type": "text"},
    {"name": "结构版本", "type": "number"},
    {"name": "搜索词", "type": "text"},
    {"name": "歌曲名", "type": "text"},
    {"name": "歌手", "type": "text"},
    {"name": "Provider", "type": "text"},
    {"name": "结果", "type": "text"},
    {"name": "耗时毫秒", "type": "number"},
    {"name": "使用时段", "type": "text"},
)


DASHBOARD_BLOCKS = (
    ("text", "说明", {"text": "# 小智使用行为分析\n数据由本地 MCP 自动采集并异步同步。"}),
    ("statistics", "事件总数", {"table_name": "原始事件", "count_all": True}),
    (
        "statistics",
        "点歌次数",
        {"table_name": "原始事件", "count_all": True, "filter": {"conjunction": "and", "conditions": [{"field_name": "事件类型", "operator": "is", "value": "music_search_started"}]}},
    ),
    ("line", "使用趋势", {"table_name": "原始事件", "count_all": True, "group_by": [{"field_name": "发生时间", "mode": "integrated", "sort": {"type": "group", "order": "asc"}}]}),
    ("ring", "事件类型分布", {"table_name": "原始事件", "count_all": True, "group_by": [{"field_name": "事件类型", "mode": "integrated"}]}),
    ("bar", "热门歌曲", {"table_name": "原始事件", "count_all": True, "group_by": [{"field_name": "歌曲名", "mode": "integrated", "sort": {"type": "value", "order": "desc"}}], "filter": {"conjunction": "and", "conditions": [{"field_name": "事件类型", "operator": "is", "value": "music_search_succeeded"}]}}),
    ("ring", "音乐来源", {"table_name": "原始事件", "count_all": True, "group_by": [{"field_name": "Provider", "mode": "integrated"}], "filter": {"conjunction": "and", "conditions": [{"field_name": "事件类型", "operator": "is", "value": "music_search_succeeded"}]}}),
    ("column", "使用时段", {"table_name": "原始事件", "count_all": True, "group_by": [{"field_name": "使用时段", "mode": "integrated"}]}),
    ("bar", "失败原因", {"table_name": "原始事件", "count_all": True, "group_by": [{"field_name": "事件类型", "mode": "integrated", "sort": {"type": "value", "order": "desc"}}], "filter": {"conjunction": "and", "conditions": [{"field_name": "结果", "operator": "is", "value": "失败"}]}}),
)


def _data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data", {})
    return data if isinstance(data, Mapping) else {}


class FeishuBaseClient:
    def __init__(self, cli: LarkCli, base_token: str, event_table_id: str = ""):
        self.cli = cli
        self.base_token = base_token.strip()
        self.event_table_id = event_table_id.strip()
        if not self.base_token:
            raise FeishuApiError("缺少 FEISHU_BASE_TOKEN")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "FeishuBaseClient":
        source = environ or os.environ
        return cls(
            LarkCli(),
            source.get("FEISHU_BASE_TOKEN", ""),
            source.get("FEISHU_EVENT_TABLE_ID", ""),
        )

    def validate_access(self) -> None:
        self.cli.run(["base", "+table-list", "--base-token", self.base_token, "--as", "user"])

    def create_event_batch(self, events: list[AnalyticsEvent]) -> dict[str, str]:
        if not events:
            return {}
        if len(events) > 500:
            raise ValueError("单批事件不能超过 500 条")
        if not self.event_table_id:
            raise FeishuApiError("缺少 FEISHU_EVENT_TABLE_ID，请先执行 analytics init")
        path = f"/open-apis/bitable/v1/apps/{self.base_token}/tables/{self.event_table_id}/records/batch_create"
        self.cli.run(
            [
                "api", "POST", path,
                "--params", json.dumps({"client_token": _batch_token(events)}, separators=(",", ":")),
                "--data", json.dumps({"records": [{"fields": event_fields(event)} for event in events]}, ensure_ascii=False, separators=(",", ":")),
                "--as", "user",
            ],
            timeout=60,
        )
        return {event.event_id: "" for event in events}

    def initialize(self) -> tuple[str, str]:
        tables = _data(self.cli.run(["base", "+table-list", "--base-token", self.base_token, "--as", "user"])).get("tables", [])
        table = next((item for item in tables if item.get("name") == "原始事件"), None)
        if table is None:
            created = self.cli.run(
                ["base", "+table-create", "--base-token", self.base_token, "--name", "原始事件", "--fields", json.dumps(EVENT_TABLE_FIELDS, ensure_ascii=False), "--as", "user"],
                timeout=60,
            )
            created_data = _data(created)
            table_id = str(created_data.get("table_id") or created_data.get("table", {}).get("id", ""))
        else:
            table_id = str(table.get("id", table.get("table_id", "")))
            self._ensure_fields(table_id)
        if not table_id:
            raise FeishuApiError("飞书 CLI 没有返回原始事件表 ID")
        self.event_table_id = table_id
        return table_id, self._ensure_dashboard()

    def _ensure_fields(self, table_id: str) -> None:
        result = self.cli.run(["base", "+field-list", "--base-token", self.base_token, "--table-id", table_id, "--as", "user"])
        existing = {item.get("name", item.get("field_name")) for item in _data(result).get("fields", [])}
        for field in EVENT_TABLE_FIELDS:
            if field["name"] in existing:
                continue
            self.cli.run(["base", "+field-create", "--base-token", self.base_token, "--table-id", table_id, "--json", json.dumps(field, ensure_ascii=False), "--as", "user"])

    def _ensure_dashboard(self) -> str:
        result = self.cli.run(["base", "+dashboard-list", "--base-token", self.base_token, "--as", "user"])
        dashboards = _data(result).get("dashboards", _data(result).get("items", []))
        existing = next((item for item in dashboards if item.get("name") == "小智使用分析"), None)
        if existing is not None:
            dashboard_id = str(existing.get("id", existing.get("dashboard_id", "")))
        else:
            created = self.cli.run(["base", "+dashboard-create", "--base-token", self.base_token, "--name", "小智使用分析", "--theme-style", "default", "--as", "user"])
            data = _data(created)
            dashboard = data.get("dashboard", {})
            dashboard_id = str(
                data.get("dashboard_id")
                or (dashboard.get("dashboard_id") if isinstance(dashboard, Mapping) else "")
                or (dashboard.get("id") if isinstance(dashboard, Mapping) else "")
            )
        if not dashboard_id:
            raise FeishuApiError("飞书 CLI 没有返回仪表盘 ID")
        self._ensure_dashboard_blocks(dashboard_id)
        return dashboard_id

    def _ensure_dashboard_blocks(self, dashboard_id: str) -> None:
        listed = self.cli.run(["base", "+dashboard-block-list", "--base-token", self.base_token, "--dashboard-id", dashboard_id, "--as", "user"])
        existing_names = {item.get("name") for item in _data(listed).get("items", [])}
        for block_type, name, data_config in DASHBOARD_BLOCKS:
            if name in existing_names:
                continue
            self.cli.run(["base", "+dashboard-block-create", "--base-token", self.base_token, "--dashboard-id", dashboard_id, "--type", block_type, "--name", name, "--data-config", json.dumps(data_config, ensure_ascii=False), "--as", "user"])


class FeishuSyncWorker:
    def __init__(self, store: AnalyticsStore, client: FeishuBaseClient, *, batch_size: int = 10, interval_seconds: float = 10):
        self.store = store
        self.client = client
        self.batch_size = max(1, min(batch_size, 20))
        self.interval_seconds = max(1, interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def from_env(cls) -> "FeishuSyncWorker":
        return cls(AnalyticsStore(default_database_path()), FeishuBaseClient.from_env(), batch_size=int(os.getenv("FEISHU_SYNC_BATCH_SIZE", "10")), interval_seconds=float(os.getenv("FEISHU_SYNC_INTERVAL_SECONDS", "10")))

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="feishu-analytics-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def sync_once(self) -> int:
        items = self.store.claim_batch(self.batch_size)
        if not items:
            return 0
        events = [item.event for item in items]
        try:
            remote_ids = self.client.create_event_batch(events)
        except LarkCliError as exc:
            self.store.release_batch([event.event_id for event in events], str(exc), delay_seconds=60)
            raise
        except Exception as exc:
            error = mask_text(str(exc))
            for item in items:
                self.store.mark_failed(item.event.event_id, error)
            raise
        self.store.mark_batch_synced(remote_ids)
        return len(events)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                count = self.sync_once()
                if count:
                    LOGGER.info("已同步 %s 条使用行为事件到飞书", count)
                    continue
            except (LarkCliError, FeishuApiError, OSError) as exc:
                LOGGER.warning("飞书统计同步暂停：%s", mask_text(str(exc)))
            self._stop.wait(self.interval_seconds)


def sync_enabled(environ: Mapping[str, str] | None = None) -> bool:
    source = environ or os.environ
    return source.get("FEISHU_ANALYTICS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def start_sync_worker() -> FeishuSyncWorker | None:
    if not sync_enabled():
        return None
    try:
        worker = FeishuSyncWorker.from_env()
        worker.start()
        return worker
    except Exception as exc:
        LOGGER.warning("无法启动飞书统计同步，事件将保留在本地：%s", mask_text(str(exc)))
        return None
