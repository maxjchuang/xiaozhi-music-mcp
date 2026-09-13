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
from usage_analytics import (
    AnalyticsEvent,
    AnalyticsStore,
    ProjectionOutboxItem,
    default_database_path,
    mask_text,
)


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


SESSION_TABLE_FIELDS = (
    {"name": "会话ID", "type": "text"},
    {"name": "设备ID", "type": "text"},
    {"name": "固件版本", "type": "text"},
    {"name": "链路ID", "type": "text"},
    {"name": "开始时间", "type": "datetime", "style": {"format": "yyyy-MM-dd HH:mm"}},
    {"name": "结束时间", "type": "datetime", "style": {"format": "yyyy-MM-dd HH:mm"}},
    {"name": "唤醒方式", "type": "text"},
    {"name": "用户原话", "type": "text"},
    {"name": "助手回复", "type": "text"},
    {"name": "对话轮数", "type": "number"},
    {"name": "触发音乐", "type": "checkbox"},
    {"name": "结果", "type": "text"},
    {"name": "错误类型", "type": "text"},
    {"name": "最后事件时间", "type": "datetime", "style": {"format": "yyyy-MM-dd HH:mm"}},
)


PLAY_TABLE_FIELDS = (
    {"name": "播放ID", "type": "text"},
    {"name": "链路ID", "type": "text"},
    {"name": "会话ID", "type": "text"},
    {"name": "设备ID", "type": "text"},
    {"name": "原始点歌文本", "type": "text"},
    {"name": "规范化搜索词", "type": "text"},
    {"name": "歌曲名", "type": "text"},
    {"name": "歌手", "type": "text"},
    {"name": "专辑", "type": "text"},
    {"name": "Provider", "type": "text"},
    {"name": "播放类型", "type": "text"},
    {"name": "请求时间", "type": "datetime", "style": {"format": "yyyy-MM-dd HH:mm"}},
    {"name": "开始时间", "type": "datetime", "style": {"format": "yyyy-MM-dd HH:mm"}},
    {"name": "结束时间", "type": "datetime", "style": {"format": "yyyy-MM-dd HH:mm"}},
    {"name": "首播等待毫秒", "type": "number"},
    {"name": "实际播放秒数", "type": "number"},
    {"name": "停止发生秒数", "type": "number"},
    {"name": "媒体时长秒数", "type": "number"},
    {"name": "原歌曲时长秒数", "type": "number"},
    {"name": "播放比例", "type": "number", "style": {"type": "plain", "precision": 2, "percentage": True}},
    {"name": "自然完播", "type": "checkbox"},
    {"name": "自然完播计数", "type": "number"},
    {"name": "快速跳过", "type": "checkbox"},
    {"name": "快速跳过计数", "type": "number"},
    {"name": "快速跳过等级", "type": "text"},
    {"name": "结束原因", "type": "text"},
    {"name": "暂停次数", "type": "number"},
    {"name": "暂停秒数", "type": "number"},
    {"name": "欠载次数", "type": "number"},
    {"name": "欠载毫秒", "type": "number"},
    {"name": "疑似搜索不满意", "type": "checkbox"},
    {"name": "不满意证据", "type": "text"},
    {"name": "后续动作", "type": "text"},
    {"name": "状态", "type": "text"},
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
    ("statistics", "实际播放次数", {"table_name": "音乐播放记录", "count_all": True}),
    ("statistics", "自然完播次数", {"table_name": "音乐播放记录", "series": [{"field_name": "自然完播计数", "rollup": "SUM"}]}),
    ("statistics", "平均播放比例", {"table_name": "音乐播放记录", "series": [{"field_name": "播放比例", "rollup": "AVERAGE"}]}),
    ("statistics", "快速跳过次数", {"table_name": "音乐播放记录", "series": [{"field_name": "快速跳过计数", "rollup": "SUM"}]}),
    ("bar", "歌曲播放次数", {"table_name": "音乐播放记录", "count_all": True, "group_by": [{"field_name": "歌曲名", "mode": "integrated", "sort": {"type": "value", "order": "desc"}}]}),
    ("bar", "歌曲平均播放比例", {"table_name": "音乐播放记录", "series": [{"field_name": "播放比例", "rollup": "AVERAGE"}], "group_by": [{"field_name": "歌曲名", "mode": "integrated", "sort": {"type": "value", "order": "desc"}}]}),
    ("ring", "播放结束原因", {"table_name": "音乐播放记录", "count_all": True, "group_by": [{"field_name": "结束原因", "mode": "integrated"}]}),
    ("column", "停止时间分布", {"table_name": "音乐播放记录", "series": [{"field_name": "停止发生秒数", "rollup": "AVERAGE"}], "group_by": [{"field_name": "快速跳过等级", "mode": "integrated"}]}),
    ("bar", "Provider 播放质量", {"table_name": "音乐播放记录", "series": [{"field_name": "播放比例", "rollup": "AVERAGE"}], "group_by": [{"field_name": "Provider", "mode": "integrated", "sort": {"type": "value", "order": "desc"}}]}),
    ("bar", "疑似搜索不满意", {"table_name": "音乐播放记录", "count_all": True, "group_by": [{"field_name": "规范化搜索词", "mode": "integrated", "sort": {"type": "value", "order": "desc"}}], "filter": {"conjunction": "and", "conditions": [{"field_name": "疑似搜索不满意", "operator": "is", "value": True}]}}),
    ("ring", "唤醒方式分布", {"table_name": "会话记录", "count_all": True, "group_by": [{"field_name": "唤醒方式", "mode": "integrated"}]}),
)

DEVICE_DASHBOARD_BLOCKS = (
    ("text", "说明", {"text": "# 小智设备稳定性\n按匿名设备和固件版本观察使用量与异常。"}),
    ("bar", "设备使用量", {"table_name": "会话记录", "count_all": True, "group_by": [{"field_name": "设备ID", "mode": "integrated", "sort": {"type": "value", "order": "desc"}}]}),
    ("bar", "固件版本使用量", {"table_name": "会话记录", "count_all": True, "group_by": [{"field_name": "固件版本", "mode": "integrated", "sort": {"type": "value", "order": "desc"}}]}),
    ("bar", "会话错误分布", {"table_name": "会话记录", "count_all": True, "group_by": [{"field_name": "错误类型", "mode": "integrated", "sort": {"type": "value", "order": "desc"}}], "filter": {"conjunction": "and", "conditions": [{"field_name": "错误类型", "operator": "isNotEmpty"}]}}),
    ("bar", "播放欠载设备", {"table_name": "音乐播放记录", "series": [{"field_name": "欠载次数", "rollup": "SUM"}], "group_by": [{"field_name": "设备ID", "mode": "integrated", "sort": {"type": "value", "order": "desc"}}]}),
)


def _optional_timestamp(value: Any) -> int | None:
    return _timestamp_milliseconds(str(value)) if value else None


def playback_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    quick_skip = bool(row.get("quick_skip_level"))
    fields = {
        "播放ID": row.get("playback_id", ""),
        "链路ID": row.get("trace_id", ""),
        "会话ID": row.get("session_id", ""),
        "设备ID": row.get("device_id", ""),
        "原始点歌文本": row.get("original_query", ""),
        "规范化搜索词": row.get("normalized_query", ""),
        "歌曲名": row.get("title", ""),
        "歌手": row.get("artist", ""),
        "专辑": row.get("album", ""),
        "Provider": row.get("provider", ""),
        "播放类型": row.get("playback_access", ""),
        "请求时间": _optional_timestamp(row.get("requested_at")),
        "开始时间": _optional_timestamp(row.get("started_at")),
        "结束时间": _optional_timestamp(row.get("ended_at")),
        "首播等待毫秒": row.get("first_audio_wait_ms", 0),
        "实际播放秒数": round(float(row.get("audible_played_ms") or 0) / 1000, 3),
        "停止发生秒数": round(float(row.get("elapsed_since_start_ms") or 0) / 1000, 3),
        "媒体时长秒数": round(float(row.get("media_duration_ms") or 0) / 1000, 3),
        "原歌曲时长秒数": round(float(row.get("song_duration_ms") or 0) / 1000, 3),
        "播放比例": row.get("play_ratio"),
        "自然完播": bool(row.get("natural_completed")),
        "自然完播计数": int(bool(row.get("natural_completed"))),
        "快速跳过": quick_skip,
        "快速跳过计数": int(quick_skip),
        "快速跳过等级": row.get("quick_skip_level", ""),
        "结束原因": row.get("end_reason", ""),
        "暂停次数": row.get("pause_count", 0),
        "暂停秒数": round(float(row.get("pause_total_ms") or 0) / 1000, 3),
        "欠载次数": row.get("underrun_count", 0),
        "欠载毫秒": row.get("underrun_total_ms", 0),
        "疑似搜索不满意": bool(row.get("suspected_search_dissatisfaction")),
        "不满意证据": row.get("dissatisfaction_reason", ""),
        "后续动作": row.get("next_action", ""),
        "状态": row.get("status", ""),
    }
    return {key: value for key, value in fields.items() if value is not None}


def session_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "会话ID": row.get("session_id", ""),
        "设备ID": row.get("device_id", ""),
        "固件版本": row.get("firmware_version", ""),
        "链路ID": row.get("trace_id", ""),
        "开始时间": _optional_timestamp(row.get("started_at")),
        "结束时间": _optional_timestamp(row.get("ended_at")),
        "唤醒方式": row.get("wake_method", ""),
        "用户原话": row.get("user_text", ""),
        "助手回复": row.get("assistant_text", ""),
        "对话轮数": row.get("turn_count", 0),
        "触发音乐": bool(row.get("triggered_music")),
        "结果": row.get("result", ""),
        "错误类型": row.get("error_type", ""),
        "最后事件时间": _optional_timestamp(row.get("last_event_at")),
    }
    return {key: value for key, value in fields.items() if value is not None}


def _data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data", {})
    return data if isinstance(data, Mapping) else {}


class FeishuBaseClient:
    def __init__(
        self,
        cli: LarkCli,
        base_token: str,
        event_table_id: str = "",
        session_table_id: str = "",
        play_table_id: str = "",
    ):
        self.cli = cli
        self.base_token = base_token.strip()
        self.event_table_id = event_table_id.strip()
        self.session_table_id = session_table_id.strip()
        self.play_table_id = play_table_id.strip()
        if not self.base_token:
            raise FeishuApiError("缺少 FEISHU_BASE_TOKEN")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "FeishuBaseClient":
        source = environ or os.environ
        return cls(
            LarkCli(),
            source.get("FEISHU_BASE_TOKEN", ""),
            source.get("FEISHU_EVENT_TABLE_ID", ""),
            source.get("FEISHU_SESSION_TABLE_ID", ""),
            source.get("FEISHU_PLAY_TABLE_ID", ""),
        )

    @classmethod
    def create_base(
        cls, name: str, *, cli: LarkCli | None = None
    ) -> tuple["FeishuBaseClient", str, str]:
        selected_cli = cli or LarkCli()
        result = selected_cli.run(
            [
                "base", "+base-create", "--name", name,
                "--time-zone", "Asia/Shanghai", "--as", "user",
            ],
            timeout=60,
        )
        data = _data(result)
        resource = data.get("base") or data.get("app") or {}
        if not isinstance(resource, Mapping):
            resource = {}
        base_token = str(
            data.get("base_token")
            or data.get("app_token")
            or resource.get("base_token")
            or resource.get("app_token")
            or resource.get("token")
            or ""
        )
        default_table_id = str(
            data.get("default_table_id") or resource.get("default_table_id") or ""
        )
        base_url = str(data.get("url") or resource.get("url") or "")
        if not base_token:
            raise FeishuApiError("飞书 CLI 创建成功但没有返回 Base Token")
        return cls(selected_cli, base_token), default_table_id, base_url

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

    def initialize(
        self, *, fresh_base: bool = False, default_table_id: str = ""
    ) -> tuple[str, str, str, str]:
        tables = _data(self.cli.run(["base", "+table-list", "--base-token", self.base_token, "--as", "user"])).get("tables", [])
        if fresh_base:
            initial_table_id = default_table_id
            if not initial_table_id and len(tables) == 1:
                initial_table_id = str(tables[0].get("id", tables[0].get("table_id", "")))
            if not initial_table_id:
                raise FeishuApiError("新建多维表格没有返回默认数据表 ID")
            self._prepare_initial_event_table(initial_table_id)
            self.event_table_id = initial_table_id
        else:
            self.event_table_id = self._ensure_table(tables, "原始事件", EVENT_TABLE_FIELDS)
        self.session_table_id = self._ensure_table(tables, "会话记录", SESSION_TABLE_FIELDS)
        self.play_table_id = self._ensure_table(tables, "音乐播放记录", PLAY_TABLE_FIELDS)
        dashboard_id = self._ensure_dashboard()
        self._ensure_named_dashboard("小智设备稳定性", DEVICE_DASHBOARD_BLOCKS)
        return (
            self.event_table_id,
            self.session_table_id,
            self.play_table_id,
            dashboard_id,
        )

    def _ensure_table(
        self, tables: list[Mapping[str, Any]], name: str, fields: tuple[dict[str, Any], ...]
    ) -> str:
        table = next((item for item in tables if item.get("name") == name), None)
        if table is None:
            created = self.cli.run(
                [
                    "base", "+table-create", "--base-token", self.base_token,
                    "--name", name, "--fields", json.dumps(fields, ensure_ascii=False),
                    "--as", "user",
                ],
                timeout=60,
            )
            created_data = _data(created)
            table_resource = created_data.get("table", {})
            table_id = str(
                created_data.get("table_id")
                or (table_resource.get("id") if isinstance(table_resource, Mapping) else "")
                or (table_resource.get("table_id") if isinstance(table_resource, Mapping) else "")
            )
        else:
            table_id = str(table.get("id", table.get("table_id", "")))
            self._ensure_fields(table_id, fields)
        if not table_id:
            raise FeishuApiError(f"飞书 CLI 没有返回{name}表 ID")
        return table_id

    def _prepare_initial_event_table(self, table_id: str) -> None:
        self.cli.run(
            [
                "base", "+table-update", "--base-token", self.base_token,
                "--table-id", table_id, "--name", "原始事件", "--as", "user",
            ]
        )
        result = self.cli.run(
            [
                "base", "+field-list", "--base-token", self.base_token,
                "--table-id", table_id, "--as", "user",
            ]
        )
        fields = list(_data(result).get("fields", []))
        existing = {item.get("name", item.get("field_name")) for item in fields}
        if "事件ID" not in existing and fields:
            primary = fields[0]
            field_id = str(primary.get("id", primary.get("field_id", "")))
            if not field_id:
                raise FeishuApiError("新建多维表格的默认主字段没有返回 ID")
            self.cli.run(
                [
                    "base", "+field-update", "--base-token", self.base_token,
                    "--table-id", table_id, "--field-id", field_id,
                    "--json", json.dumps(EVENT_TABLE_FIELDS[0], ensure_ascii=False),
                    "--as", "user", "--yes",
                ]
            )
            existing.add("事件ID")
        self._ensure_fields(table_id, EVENT_TABLE_FIELDS, existing=existing)

    def _ensure_fields(
        self,
        table_id: str,
        fields: tuple[dict[str, Any], ...],
        *,
        existing: set[object] | None = None,
    ) -> None:
        if existing is None:
            result = self.cli.run(["base", "+field-list", "--base-token", self.base_token, "--table-id", table_id, "--as", "user"])
            existing = {item.get("name", item.get("field_name")) for item in _data(result).get("fields", [])}
        for field in fields:
            if field["name"] in existing:
                continue
            self.cli.run(["base", "+field-create", "--base-token", self.base_token, "--table-id", table_id, "--json", json.dumps(field, ensure_ascii=False), "--as", "user"])

    @staticmethod
    def _record_id(payload: Mapping[str, Any]) -> str:
        data = _data(payload)
        record = data.get("record", {})
        record_ids = data.get("record_id_list", [])
        return str(
            data.get("record_id")
            or (record.get("record_id") if isinstance(record, Mapping) else "")
            or (record.get("id") if isinstance(record, Mapping) else "")
            or (record_ids[0] if isinstance(record_ids, list) and record_ids else "")
            or ""
        )

    def _find_projection_record(self, item: ProjectionOutboxItem, primary_field: str) -> str:
        result = self.cli.run(
            [
                "base", "+record-search", "--base-token", self.base_token,
                "--table-id", self.play_table_id if item.entity_type == "playback" else self.session_table_id,
                "--json", json.dumps(
                    {
                        # record-search limits keyword to 50 characters, while the
                        # stored primary field may be longer. Search by prefix and
                        # verify the full value from the returned row below.
                        "keyword": item.entity_id[:50],
                        "search_fields": [primary_field],
                        "select_fields": [primary_field],
                        "limit": 10,
                    },
                    ensure_ascii=False,
                ),
                "--format", "json", "--as", "user",
            ]
        )
        data = _data(result)
        records = data.get("records", data.get("items", []))
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, Mapping):
                continue
            fields = record.get("fields", {})
            if isinstance(fields, Mapping) and str(fields.get(primary_field, "")) == item.entity_id:
                return str(record.get("record_id") or record.get("id") or "")
        columns = data.get("fields", [])
        rows = data.get("data", [])
        record_ids = data.get("record_id_list", [])
        if (
            isinstance(columns, list)
            and primary_field in columns
            and isinstance(rows, list)
            and isinstance(record_ids, list)
        ):
            primary_index = columns.index(primary_field)
            for row, record_id in zip(rows, record_ids):
                if (
                    isinstance(row, list)
                    and primary_index < len(row)
                    and str(row[primary_index]) == item.entity_id
                ):
                    return str(record_id)
        return ""

    def upsert_projection(self, item: ProjectionOutboxItem) -> str:
        if item.entity_type == "playback":
            table_id = self.play_table_id
            primary_field = "播放ID"
            fields = playback_fields(item.fields)
        elif item.entity_type == "session":
            table_id = self.session_table_id
            primary_field = "会话ID"
            fields = session_fields(item.fields)
        else:
            raise FeishuApiError(f"不支持的投影类型：{item.entity_type}")
        if not table_id:
            raise FeishuApiError("缺少飞书业务表 ID，请重新执行 analytics init")
        record_id = item.remote_record_id or self._find_projection_record(item, primary_field)
        arguments = [
            "base", "+record-upsert", "--base-token", self.base_token,
            "--table-id", table_id,
        ]
        if record_id:
            arguments.extend(["--record-id", record_id])
        arguments.extend(["--json", json.dumps(fields, ensure_ascii=False), "--as", "user"])
        result = self.cli.run(arguments, timeout=60)
        returned_id = self._record_id(result)
        resolved_id = returned_id or record_id
        if not resolved_id:
            # Current lark-cli create responses can contain only
            # data.record.create plus created=true and omit the new ID. Recover
            # it by the verified business key before acknowledging the outbox.
            resolved_id = self._find_projection_record(item, primary_field)
        if not resolved_id:
            raise FeishuApiError(f"飞书没有返回{item.entity_type}记录 ID")
        return resolved_id

    def _ensure_dashboard(self) -> str:
        return self._ensure_named_dashboard("小智使用分析", DASHBOARD_BLOCKS)

    def _ensure_named_dashboard(
        self, name: str, blocks: tuple[tuple[str, str, dict[str, Any]], ...]
    ) -> str:
        result = self.cli.run(["base", "+dashboard-list", "--base-token", self.base_token, "--as", "user"])
        dashboards = _data(result).get("dashboards", _data(result).get("items", []))
        existing = next((item for item in dashboards if item.get("name") == name), None)
        if existing is not None:
            dashboard_id = str(existing.get("id", existing.get("dashboard_id", "")))
        else:
            created = self.cli.run(["base", "+dashboard-create", "--base-token", self.base_token, "--name", name, "--theme-style", "default", "--as", "user"])
            data = _data(created)
            dashboard = data.get("dashboard", {})
            dashboard_id = str(
                data.get("dashboard_id")
                or (dashboard.get("dashboard_id") if isinstance(dashboard, Mapping) else "")
                or (dashboard.get("id") if isinstance(dashboard, Mapping) else "")
            )
        if not dashboard_id:
            raise FeishuApiError("飞书 CLI 没有返回仪表盘 ID")
        self._ensure_dashboard_blocks(dashboard_id, blocks)
        return dashboard_id

    def _ensure_dashboard_blocks(
        self, dashboard_id: str, blocks: tuple[tuple[str, str, dict[str, Any]], ...]
    ) -> None:
        listed = self.cli.run(["base", "+dashboard-block-list", "--base-token", self.base_token, "--dashboard-id", dashboard_id, "--as", "user"])
        existing_names = {item.get("name") for item in _data(listed).get("items", [])}
        for block_type, name, data_config in blocks:
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
        synced = 0
        if items:
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
            synced += len(events)

        projection_items = self.store.claim_projection_batch(self.batch_size)
        for index, item in enumerate(projection_items):
            try:
                remote_id = self.client.upsert_projection(item)
            except LarkCliError as exc:
                self.store.release_projection_batch(
                    projection_items[index:], str(exc), delay_seconds=60
                )
                raise
            except Exception as exc:
                self.store.mark_projection_failed(item, mask_text(str(exc)))
                for remaining in projection_items[index + 1:]:
                    self.store.release_projection_batch([remaining], "batch interrupted", delay_seconds=0)
                raise
            self.store.mark_projection_synced(
                item.entity_type, item.entity_id, item.revision, remote_id
            )
            synced += 1
        return synced

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
        try:
            raw_retention_days = int(os.getenv("ANALYTICS_RAW_RETENTION_DAYS", "30"))
            projection_retention_days = int(
                os.getenv("ANALYTICS_PROJECTION_RETENTION_DAYS", "180")
            )
        except ValueError:
            LOGGER.warning("统计保留期配置不是整数，回退为 30/180 天")
            raw_retention_days, projection_retention_days = 30, 180
        store = AnalyticsStore(default_database_path())
        store.cleanup_retention(
            raw_days=raw_retention_days,
            projection_days=projection_retention_days,
        )
        worker = FeishuSyncWorker(
            store,
            FeishuBaseClient.from_env(),
            batch_size=int(os.getenv("FEISHU_SYNC_BATCH_SIZE", "10")),
            interval_seconds=float(os.getenv("FEISHU_SYNC_INTERVAL_SECONDS", "10")),
        )
        worker.start()
        return worker
    except Exception as exc:
        LOGGER.warning("无法启动飞书统计同步，事件将保留在本地：%s", mask_text(str(exc)))
        return None
