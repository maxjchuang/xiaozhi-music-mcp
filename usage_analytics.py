#!/usr/bin/env python3
"""Local-first usage analytics primitives for the music MCP service."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Mapping
import uuid


LOGGER = logging.getLogger("xiaozhi-analytics")
SCHEMA_VERSION = 1
TRANSCRIPT_FIELDS = frozenset({"user_text", "assistant_text"})
VALID_TRANSCRIPT_MODES = frozenset({"off", "masked", "full"})
PLAYBACK_EVENT_TYPES = frozenset(
    {
        "playback_started",
        "playback_paused",
        "playback_resumed",
        "playback_stopped",
        "playback_completed",
        "playback_failed",
        "song_switched",
        "audio_underrun",
        "decode_error",
        "network_error",
    }
)
PLAYBACK_TERMINAL_TYPES = frozenset(
    {"playback_stopped", "playback_completed", "playback_failed", "song_switched", "decode_error", "network_error"}
)
SESSION_EVENT_TYPES = frozenset(
    {
        "wake_detected",
        "listening_started",
        "listening_stopped",
        "user_utterance",
        "assistant_response",
        *PLAYBACK_EVENT_TYPES,
    }
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)(token|cookie|app_secret)\s*[:=]\s*([^\s,;&]+)"),
    re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+"),
)
_PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_EMAIL_PATTERN = re.compile(r"(?<![\w.+-])([\w.+-]{1,64})@([\w.-]+\.[A-Za-z]{2,})(?![\w.-])")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def analytics_enabled(environ: Mapping[str, str] | None = None) -> bool:
    source = environ or os.environ
    return source.get("ANALYTICS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def transcript_mode(environ: Mapping[str, str] | None = None) -> str:
    source = environ or os.environ
    mode = source.get("ANALYTICS_TRANSCRIPT_MODE", "masked").strip().lower()
    return mode if mode in VALID_TRANSCRIPT_MODES else "masked"


def default_database_path(environ: Mapping[str, str] | None = None) -> Path:
    source = environ or os.environ
    configured = source.get("ANALYTICS_DATABASE_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "xiaozhi" / "analytics.sqlite3"


def mask_text(value: str) -> str:
    """Redact common credentials and personal identifiers from free-form text."""
    masked = value
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)(Bearer"):
            masked = pattern.sub(r"\1***", masked)
        else:
            masked = pattern.sub(lambda match: f"{match.group(1)}=***", masked)
    masked = _PHONE_PATTERN.sub("1**********", masked)
    masked = _EMAIL_PATTERN.sub(lambda match: f"{match.group(1)[:1]}***@{match.group(2)}", masked)
    return masked


def sanitize_payload(payload: Mapping[str, Any], mode: str) -> dict[str, Any]:
    selected_mode = mode if mode in VALID_TRANSCRIPT_MODES else "masked"

    def sanitize(key: str, value: Any) -> Any:
        if key in TRANSCRIPT_FIELDS and selected_mode == "off":
            return None
        if isinstance(value, str):
            if key in TRANSCRIPT_FIELDS and selected_mode == "full":
                return value[:4000]
            return mask_text(value)[:4000]
        if isinstance(value, Mapping):
            return {str(child_key): sanitize(str(child_key), child) for child_key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [sanitize(key, item) for item in value[:100]]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return mask_text(str(value))[:4000]

    return {
        str(key): sanitized
        for key, value in payload.items()
        if (sanitized := sanitize(str(key), value)) is not None
    }


def hash_device_id(device_id: str, salt: str) -> str:
    if not device_id:
        return ""
    return hashlib.sha256(f"{salt}:{device_id}".encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class AnalyticsEvent:
    event_id: str
    event_type: str
    occurred_at: str
    received_at: str
    source: str
    device_id: str = ""
    session_id: str = ""
    trace_id: str = ""
    payload: Mapping[str, Any] | None = None
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        event_type: str,
        *,
        source: str,
        device_id: str = "",
        session_id: str = "",
        trace_id: str = "",
        payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        occurred_at: datetime | None = None,
        privacy_mode: str = "masked",
        device_salt: str = "xiaozhi-local",
    ) -> "AnalyticsEvent":
        if not event_type.strip():
            raise ValueError("event_type cannot be empty")
        now = isoformat_utc()
        return cls(
            event_id=event_id or str(uuid.uuid4()),
            event_type=event_type.strip(),
            occurred_at=isoformat_utc(occurred_at) if occurred_at else now,
            received_at=now,
            source=source.strip() or "unknown",
            device_id=hash_device_id(device_id, device_salt),
            session_id=session_id[:128],
            trace_id=trace_id[:128],
            payload=sanitize_payload(payload or {}, privacy_mode),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OutboxItem:
    event: AnalyticsEvent
    attempts: int


@dataclass(frozen=True, slots=True)
class ProjectionOutboxItem:
    entity_type: str
    entity_id: str
    revision: int
    remote_record_id: str
    fields: Mapping[str, Any]
    attempts: int


class AnalyticsStore:
    """SQLite event store with an idempotent transactional outbox."""

    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._schema_lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    device_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    trace_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at);
                CREATE INDEX IF NOT EXISTS idx_events_session_id ON events(session_id);
                CREATE INDEX IF NOT EXISTS idx_events_trace_id ON events(trace_id);

                CREATE TABLE IF NOT EXISTS sync_outbox (
                    event_id TEXT PRIMARY KEY REFERENCES events(event_id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    remote_record_id TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_ready
                    ON sync_outbox(status, next_attempt_at, updated_at);

                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS playbacks (
                    playback_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL DEFAULT '',
                    device_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    artist TEXT NOT NULL DEFAULT '',
                    album TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    cache_hit INTEGER NOT NULL DEFAULT 0,
                    cache_id TEXT NOT NULL DEFAULT '',
                    delivery_source TEXT NOT NULL DEFAULT 'network',
                    original_query TEXT NOT NULL DEFAULT '',
                    normalized_query TEXT NOT NULL DEFAULT '',
                    playback_access TEXT NOT NULL DEFAULT 'full',
                    media_duration_ms INTEGER,
                    song_duration_ms INTEGER,
                    requested_at TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    ended_at TEXT NOT NULL DEFAULT '',
                    first_audio_wait_ms INTEGER NOT NULL DEFAULT 0,
                    audible_played_ms INTEGER NOT NULL DEFAULT 0,
                    elapsed_since_start_ms INTEGER NOT NULL DEFAULT 0,
                    pause_total_ms INTEGER NOT NULL DEFAULT 0,
                    pause_count INTEGER NOT NULL DEFAULT 0,
                    underrun_count INTEGER NOT NULL DEFAULT 0,
                    underrun_total_ms INTEGER NOT NULL DEFAULT 0,
                    end_reason TEXT NOT NULL DEFAULT '',
                    natural_completed INTEGER NOT NULL DEFAULT 0,
                    quick_skip_level TEXT NOT NULL DEFAULT '',
                    suspected_search_dissatisfaction INTEGER NOT NULL DEFAULT 0,
                    dissatisfaction_reason TEXT NOT NULL DEFAULT '',
                    next_action TEXT NOT NULL DEFAULT '',
                    play_ratio REAL,
                    status TEXT NOT NULL DEFAULT 'requested',
                    revision INTEGER NOT NULL DEFAULT 0,
                    last_monotonic_ms INTEGER NOT NULL DEFAULT -1,
                    last_event_at TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_playbacks_trace_id ON playbacks(trace_id);
                CREATE INDEX IF NOT EXISTS idx_playbacks_started_at ON playbacks(started_at);

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL DEFAULT '',
                    firmware_version TEXT NOT NULL DEFAULT '',
                    trace_id TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    ended_at TEXT NOT NULL DEFAULT '',
                    wake_method TEXT NOT NULL DEFAULT '',
                    user_text TEXT NOT NULL DEFAULT '',
                    assistant_text TEXT NOT NULL DEFAULT '',
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    triggered_music INTEGER NOT NULL DEFAULT 0,
                    result TEXT NOT NULL DEFAULT '',
                    error_type TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 0,
                    last_monotonic_ms INTEGER NOT NULL DEFAULT -1,
                    last_event_at TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at);

                CREATE TABLE IF NOT EXISTS projection_outbox (
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    remote_record_id TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(entity_type, entity_id)
                );
                CREATE INDEX IF NOT EXISTS idx_projection_outbox_status
                    ON projection_outbox(status, updated_at);
                """
            )
            self._ensure_columns(
                connection,
                "playbacks",
                {
                    "original_query": "TEXT NOT NULL DEFAULT ''",
                    "normalized_query": "TEXT NOT NULL DEFAULT ''",
                    "suspected_search_dissatisfaction": "INTEGER NOT NULL DEFAULT 0",
                    "dissatisfaction_reason": "TEXT NOT NULL DEFAULT ''",
                    "next_action": "TEXT NOT NULL DEFAULT ''",
                    "first_audio_wait_ms": "INTEGER NOT NULL DEFAULT 0",
                    "cache_hit": "INTEGER NOT NULL DEFAULT 0",
                    "cache_id": "TEXT NOT NULL DEFAULT ''",
                    "delivery_source": "TEXT NOT NULL DEFAULT 'network'",
                },
            )
            self._ensure_columns(
                connection,
                "projection_outbox",
                {"next_attempt_at": "REAL NOT NULL DEFAULT 0"},
            )

    @staticmethod
    def _ensure_columns(
        connection: sqlite3.Connection, table: str, columns: Mapping[str, str]
    ) -> None:
        existing = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, declaration in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def append(self, event: AnalyticsEvent) -> bool:
        now = time.time()
        payload_json = json.dumps(event.payload or {}, ensure_ascii=False, separators=(",", ":"))
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO events (
                    event_id, event_type, occurred_at, received_at, source,
                    device_id, session_id, trace_id, payload_json, schema_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.event_type,
                    event.occurred_at,
                    event.received_at,
                    event.source,
                    event.device_id,
                    event.session_id,
                    event.trace_id,
                    payload_json,
                    event.schema_version,
                    now,
                ),
            )
            if cursor.rowcount == 0:
                return False
            connection.execute(
                "INSERT INTO sync_outbox(event_id, updated_at) VALUES (?, ?)",
                (event.event_id, now),
            )
            self._apply_playback_projection(connection, event, now)
            self._apply_session_projection(connection, event, now)
            self._refresh_search_dissatisfaction(connection, event, now)
        return True

    @staticmethod
    def _optional_nonnegative_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
        return None

    @staticmethod
    def _quick_skip_level(end_reason: str, audible_ms: int, media_duration_ms: int | None) -> str:
        if end_reason not in {"user_stopped", "song_switched"}:
            return ""
        try:
            immediate_seconds = float(os.getenv("ANALYTICS_IMMEDIATE_SKIP_SECONDS", "10"))
        except ValueError:
            immediate_seconds = 10
        try:
            early_seconds = float(os.getenv("ANALYTICS_QUICK_SKIP_SECONDS", "30"))
        except ValueError:
            early_seconds = 30
        immediate_ms = max(1, int(immediate_seconds * 1000))
        early_ms = max(immediate_ms, int(early_seconds * 1000))
        if audible_ms <= immediate_ms:
            return "immediate"
        if audible_ms <= early_ms:
            return "early"
        if media_duration_ms and audible_ms / media_duration_ms < 0.25:
            return "low_ratio"
        return ""

    def _apply_playback_projection(
        self, connection: sqlite3.Connection, event: AnalyticsEvent, now: float
    ) -> None:
        if event.event_type not in PLAYBACK_EVENT_TYPES:
            return
        payload = event.payload or {}
        playback_id = str(payload.get("playback_id", "")).strip()[:128]
        if not playback_id:
            return
        existing_row = connection.execute(
            "SELECT * FROM playbacks WHERE playback_id = ?", (playback_id,)
        ).fetchone()
        existing = dict(existing_row) if existing_row is not None else {}

        search_context: dict[str, Any] = {}
        if event.trace_id:
            search_row = connection.execute(
                """
                SELECT payload_json, occurred_at FROM events
                WHERE trace_id = ? AND event_type IN ('music_search_succeeded', 'music_search_started')
                ORDER BY CASE event_type WHEN 'music_search_succeeded' THEN 0 ELSE 1 END, created_at DESC
                LIMIT 1
                """,
                (event.trace_id,),
            ).fetchone()
            if search_row is not None:
                search_context = json.loads(search_row["payload_json"])
                search_context["_occurred_at"] = search_row["occurred_at"]

        incoming_clock = self._optional_nonnegative_int(payload.get("monotonic_ms"))
        previous_clock = int(existing.get("last_monotonic_ms", -1))
        lifecycle_is_current = incoming_clock is None or incoming_clock >= previous_clock
        event_type = event.event_type

        def text(name: str, fallback: str = "") -> str:
            value = payload.get(name)
            return str(value)[:500] if value not in {None, ""} else str(existing.get(name, fallback))

        media_duration = self._optional_nonnegative_int(payload.get("media_duration_ms"))
        if media_duration is None:
            media_duration = existing.get("media_duration_ms")
        song_duration = self._optional_nonnegative_int(payload.get("song_duration_ms"))
        if song_duration is None:
            song_duration = existing.get("song_duration_ms") or media_duration

        audible_ms = max(
            int(existing.get("audible_played_ms", 0)),
            self._optional_nonnegative_int(payload.get("audible_played_ms")) or 0,
        )
        elapsed_ms = max(
            int(existing.get("elapsed_since_start_ms", 0)),
            self._optional_nonnegative_int(payload.get("elapsed_since_start_ms")) or 0,
        )
        pause_total_ms = max(
            int(existing.get("pause_total_ms", 0)),
            self._optional_nonnegative_int(payload.get("pause_total_ms")) or 0,
        )
        pause_count = int(existing.get("pause_count", 0))
        underrun_count = int(existing.get("underrun_count", 0))
        underrun_total_ms = int(existing.get("underrun_total_ms", 0))
        if event_type == "playback_paused" and lifecycle_is_current:
            pause_count += 1
        if event_type == "audio_underrun":
            underrun_count += 1
            underrun_total_ms += self._optional_nonnegative_int(payload.get("wait_ms")) or 0
        reported_underruns = self._optional_nonnegative_int(payload.get("underrun_count"))
        reported_underrun_ms = self._optional_nonnegative_int(payload.get("underrun_total_ms"))
        if reported_underruns is not None:
            underrun_count = max(underrun_count, reported_underruns)
        if reported_underrun_ms is not None:
            underrun_total_ms = max(underrun_total_ms, reported_underrun_ms)

        requested_at = str(existing.get("requested_at", ""))
        started_at = str(existing.get("started_at", ""))
        ended_at = str(existing.get("ended_at", ""))
        first_audio_wait_ms = int(existing.get("first_audio_wait_ms", 0))
        status = str(existing.get("status", "requested"))
        end_reason = str(existing.get("end_reason", ""))
        if event_type == "playback_started":
            requested_at = requested_at or str(search_context.get("_occurred_at") or event.occurred_at)
            started_at = started_at or event.occurred_at
            try:
                first_audio_wait_ms = max(
                    0,
                    round(
                        (datetime.fromisoformat(started_at) - datetime.fromisoformat(requested_at)).total_seconds()
                        * 1000
                    ),
                )
            except ValueError:
                first_audio_wait_ms = 0
            if lifecycle_is_current:
                status = "playing"
                end_reason = ""
                ended_at = ""
        elif lifecycle_is_current:
            if event_type == "playback_paused":
                status = "paused"
            elif event_type == "playback_resumed":
                status = "playing"
            elif event_type in PLAYBACK_TERMINAL_TYPES:
                ended_at = event.occurred_at
                if event_type == "playback_completed":
                    end_reason = "natural_completed"
                    status = "completed"
                elif event_type == "playback_stopped":
                    end_reason = str(payload.get("end_reason") or "user_stopped")[:100]
                    status = "stopped"
                elif event_type == "song_switched":
                    end_reason = "song_switched"
                    status = "stopped"
                elif event_type == "decode_error":
                    end_reason = "decode_failed"
                    status = "failed"
                elif event_type == "network_error":
                    end_reason = "network_failed"
                    status = "failed"
                else:
                    end_reason = str(payload.get("end_reason") or "unknown")[:100]
                    status = "failed"

        play_ratio = min(1.0, audible_ms / media_duration) if media_duration else None
        quick_skip_level = self._quick_skip_level(end_reason, audible_ms, media_duration)
        natural_completed = int(end_reason == "natural_completed")
        cache_hit = int(
            bool(payload.get("cache_hit"))
            or bool(search_context.get("cache_hit"))
            or bool(existing.get("cache_hit", 0))
        )
        cache_id = str(
            payload.get("cache_id")
            or search_context.get("cache_id")
            or existing.get("cache_id", "")
        )[:128]
        delivery_source = str(
            payload.get("delivery_source")
            or search_context.get("delivery_source")
            or existing.get("delivery_source", "cache" if cache_hit else "network")
        )[:50]
        revision = int(existing.get("revision", 0)) + 1
        last_clock = max(previous_clock, incoming_clock if incoming_clock is not None else previous_clock)
        values = (
            playback_id,
            event.trace_id or str(existing.get("trace_id", "")),
            event.device_id or str(existing.get("device_id", "")),
            event.session_id or str(existing.get("session_id", "")),
            text("title"), text("artist"), text("album"), text("provider"),
            cache_hit, cache_id, delivery_source,
            str(search_context.get("query") or existing.get("original_query", ""))[:500],
            str(search_context.get("normalized_query") or search_context.get("query") or existing.get("normalized_query", ""))[:500],
            text("playback_access", "full"), media_duration, song_duration,
            requested_at, started_at, ended_at, first_audio_wait_ms, audible_ms, elapsed_ms,
            pause_total_ms, pause_count, underrun_count, underrun_total_ms,
            end_reason, natural_completed, quick_skip_level,
            int(existing.get("suspected_search_dissatisfaction", 0)),
            str(existing.get("dissatisfaction_reason", "")),
            str(existing.get("next_action", "")),
            play_ratio, status,
            revision, last_clock, event.occurred_at, now,
        )
        connection.execute(
            """
            INSERT INTO playbacks (
                playback_id, trace_id, device_id, session_id, title, artist, album,
                provider, cache_hit, cache_id, delivery_source,
                original_query, normalized_query, playback_access,
                media_duration_ms, song_duration_ms,
                requested_at, started_at, ended_at, first_audio_wait_ms, audible_played_ms,
                elapsed_since_start_ms, pause_total_ms, pause_count, underrun_count,
                underrun_total_ms, end_reason, natural_completed, quick_skip_level,
                suspected_search_dissatisfaction, dissatisfaction_reason, next_action,
                play_ratio, status, revision, last_monotonic_ms, last_event_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(playback_id) DO UPDATE SET
                trace_id=excluded.trace_id, device_id=excluded.device_id,
                session_id=excluded.session_id, title=excluded.title, artist=excluded.artist,
                album=excluded.album, provider=excluded.provider,
                cache_hit=excluded.cache_hit, cache_id=excluded.cache_id,
                delivery_source=excluded.delivery_source,
                original_query=excluded.original_query,
                normalized_query=excluded.normalized_query,
                playback_access=excluded.playback_access,
                media_duration_ms=excluded.media_duration_ms,
                song_duration_ms=excluded.song_duration_ms,
                requested_at=excluded.requested_at, started_at=excluded.started_at,
                ended_at=excluded.ended_at, first_audio_wait_ms=excluded.first_audio_wait_ms,
                audible_played_ms=excluded.audible_played_ms,
                elapsed_since_start_ms=excluded.elapsed_since_start_ms,
                pause_total_ms=excluded.pause_total_ms, pause_count=excluded.pause_count,
                underrun_count=excluded.underrun_count,
                underrun_total_ms=excluded.underrun_total_ms,
                end_reason=excluded.end_reason, natural_completed=excluded.natural_completed,
                quick_skip_level=excluded.quick_skip_level, play_ratio=excluded.play_ratio,
                suspected_search_dissatisfaction=excluded.suspected_search_dissatisfaction,
                dissatisfaction_reason=excluded.dissatisfaction_reason,
                next_action=excluded.next_action,
                status=excluded.status, revision=excluded.revision,
                last_monotonic_ms=excluded.last_monotonic_ms,
                last_event_at=excluded.last_event_at, updated_at=excluded.updated_at
            """,
            values,
        )
        connection.execute(
            """
            INSERT INTO projection_outbox (
                entity_type, entity_id, revision, status, attempts, next_attempt_at, updated_at
            ) VALUES ('playback', ?, ?, 'pending', 0, 0, ?)
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                revision=excluded.revision, status='pending', attempts=0,
                next_attempt_at=0, last_error='', updated_at=excluded.updated_at
            """,
            (playback_id, revision, now),
        )

    def _apply_session_projection(
        self, connection: sqlite3.Connection, event: AnalyticsEvent, now: float
    ) -> None:
        if event.event_type not in SESSION_EVENT_TYPES or not event.session_id:
            return
        payload = event.payload or {}
        existing_row = connection.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (event.session_id,)
        ).fetchone()
        existing = dict(existing_row) if existing_row is not None else {}
        incoming_clock = self._optional_nonnegative_int(payload.get("monotonic_ms"))
        previous_clock = int(existing.get("last_monotonic_ms", -1))
        is_current = incoming_clock is None or incoming_clock >= previous_clock
        started_at = str(existing.get("started_at", "")) or event.occurred_at
        ended_at = str(existing.get("ended_at", ""))
        if event.event_type == "listening_stopped" and is_current:
            ended_at = event.occurred_at
        turn_count = int(existing.get("turn_count", 0))
        if event.event_type == "user_utterance" and is_current:
            turn_count += 1
        user_text = str(existing.get("user_text", ""))
        assistant_text = str(existing.get("assistant_text", ""))
        if event.event_type == "user_utterance" and is_current:
            user_text = self._append_session_text(
                user_text, str(payload.get("user_text", ""))
            )
        elif event.event_type == "assistant_response" and is_current:
            assistant_text = self._append_session_text(
                assistant_text, str(payload.get("assistant_text", ""))
            )
        result = str(existing.get("result", ""))
        error_type = str(existing.get("error_type", ""))
        if event.event_type in {"network_error", "decode_error", "playback_failed"}:
            result = "failed"
            error_type = str(payload.get("error_type") or payload.get("end_reason") or event.event_type)[:200]
        elif event.event_type == "playback_completed":
            result = "completed"
        elif event.event_type == "playback_stopped":
            result = "stopped"
        elif event.event_type == "song_switched":
            result = "switched"
        revision = int(existing.get("revision", 0)) + 1
        values = (
            event.session_id,
            event.device_id or str(existing.get("device_id", "")),
            str(payload.get("firmware_version") or existing.get("firmware_version", ""))[:200],
            event.trace_id or str(existing.get("trace_id", "")),
            started_at,
            ended_at,
            str(payload.get("wake_method") or existing.get("wake_method", ""))[:100],
            user_text,
            assistant_text,
            turn_count,
            int(bool(existing.get("triggered_music", 0)) or event.event_type == "playback_started"),
            result,
            error_type,
            revision,
            max(previous_clock, incoming_clock if incoming_clock is not None else previous_clock),
            event.occurred_at,
            now,
        )
        connection.execute(
            """
            INSERT INTO sessions (
                session_id, device_id, firmware_version, trace_id, started_at, ended_at,
                wake_method, user_text, assistant_text, turn_count, triggered_music,
                result, error_type, revision, last_monotonic_ms, last_event_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                device_id=excluded.device_id, firmware_version=excluded.firmware_version,
                trace_id=excluded.trace_id, started_at=excluded.started_at,
                ended_at=excluded.ended_at, wake_method=excluded.wake_method,
                user_text=excluded.user_text, assistant_text=excluded.assistant_text,
                turn_count=excluded.turn_count, triggered_music=excluded.triggered_music,
                result=excluded.result, error_type=excluded.error_type,
                revision=excluded.revision, last_monotonic_ms=excluded.last_monotonic_ms,
                last_event_at=excluded.last_event_at, updated_at=excluded.updated_at
            """,
            values,
        )
        connection.execute(
            """
            INSERT INTO projection_outbox (
                entity_type, entity_id, revision, status, attempts, next_attempt_at, updated_at
            ) VALUES ('session', ?, ?, 'pending', 0, 0, ?)
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                revision=excluded.revision, status='pending', attempts=0,
                next_attempt_at=0, last_error='', updated_at=excluded.updated_at
            """,
            (event.session_id, revision, now),
        )

    @staticmethod
    def _append_session_text(existing: str, incoming: str) -> str:
        value = incoming.strip()
        if not value:
            return existing[:4000]
        parts = existing.split("\n") if existing else []
        if parts and parts[-1] == value:
            return existing[:4000]
        return "\n".join([*parts, value])[-4000:]

    def _refresh_search_dissatisfaction(
        self, connection: sqlite3.Connection, event: AnalyticsEvent, now: float
    ) -> None:
        if event.event_type != "music_search_started":
            return
        current_query = str((event.payload or {}).get("query", "")).strip()
        rows = connection.execute(
            """
            SELECT playback_id, original_query, ended_at FROM playbacks
            WHERE quick_skip_level != '' AND ended_at != ''
              AND suspected_search_dissatisfaction = 0
            ORDER BY ended_at DESC LIMIT 10
            """
        ).fetchall()
        current_time = datetime.fromisoformat(event.occurred_at)
        for row in rows:
            ended_time = datetime.fromisoformat(row["ended_at"])
            delta = (current_time - ended_time).total_seconds()
            if delta < 0 or delta > 60:
                continue
            reason = "same_query_retried" if current_query and current_query == row["original_query"] else "new_query_after_skip"
            connection.execute(
                """
                UPDATE playbacks SET suspected_search_dissatisfaction=1,
                    dissatisfaction_reason=?, next_action='searched_again',
                    revision=revision+1, updated_at=? WHERE playback_id=?
                """,
                (reason, now, row["playback_id"]),
            )
            revision = connection.execute(
                "SELECT revision FROM playbacks WHERE playback_id=?", (row["playback_id"],)
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO projection_outbox (
                    entity_type, entity_id, revision, status, attempts, next_attempt_at, updated_at
                ) VALUES ('playback', ?, ?, 'pending', 0, 0, ?)
                ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                    revision=excluded.revision, status='pending', attempts=0,
                    next_attempt_at=0, last_error='', updated_at=excluded.updated_at
                """,
                (row["playback_id"], revision, now),
            )
            break

    def playback_status(self) -> dict[str, int]:
        result = {"playbacks": 0, "sessions": 0, "pending": 0, "sending": 0, "synced": 0, "dead": 0}
        with self._connection() as connection:
            result["playbacks"] = int(connection.execute("SELECT COUNT(*) FROM playbacks").fetchone()[0])
            result["sessions"] = int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM projection_outbox GROUP BY status"
            ):
                result[str(row["status"])] = int(row["count"])
        return result

    def get_playback(self, playback_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM playbacks WHERE playback_id = ?", (playback_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def query_sessions(
        self,
        *,
        device_id: str = "",
        since: str = "",
        until: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if device_id:
            clauses.append("device_id = ?")
            parameters.append(device_id)
        if since:
            clauses.append("started_at >= ?")
            parameters.append(since)
        if until:
            clauses.append("started_at <= ?")
            parameters.append(until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(1, min(limit, 1000)))
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM sessions {where} ORDER BY started_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_session(self, session_id: str) -> dict[str, int]:
        """Delete one session and its local raw/playback projections."""
        with self._connection() as connection:
            playback_ids = [
                str(row["playback_id"])
                for row in connection.execute(
                    "SELECT playback_id FROM playbacks WHERE session_id = ?", (session_id,)
                )
            ]
            if playback_ids:
                placeholders = ",".join("?" for _ in playback_ids)
                connection.execute(
                    f"DELETE FROM projection_outbox WHERE entity_type='playback' "
                    f"AND entity_id IN ({placeholders})",
                    playback_ids,
                )
            connection.execute(
                "DELETE FROM projection_outbox WHERE entity_type='session' AND entity_id=?",
                (session_id,),
            )
            deleted_playbacks = connection.execute(
                "DELETE FROM playbacks WHERE session_id = ?", (session_id,)
            ).rowcount
            deleted_sessions = connection.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            ).rowcount
            deleted_events = connection.execute(
                "DELETE FROM events WHERE session_id = ?", (session_id,)
            ).rowcount
        return {
            "events": int(deleted_events),
            "playbacks": int(deleted_playbacks),
            "sessions": int(deleted_sessions),
        }

    def cleanup_retention(
        self, *, raw_days: int = 30, projection_days: int = 180
    ) -> dict[str, int]:
        """Remove only synced rows older than the configured retention windows."""
        raw_cutoff = isoformat_utc(utc_now() - timedelta(days=max(1, raw_days)))
        projection_cutoff = isoformat_utc(
            utc_now() - timedelta(days=max(1, projection_days))
        )
        with self._connection() as connection:
            old_playbacks = [
                str(row["playback_id"])
                for row in connection.execute(
                    """
                    SELECT p.playback_id FROM playbacks AS p
                    JOIN projection_outbox AS o
                      ON o.entity_type='playback' AND o.entity_id=p.playback_id
                    WHERE o.status='synced' AND p.last_event_at != ''
                      AND p.last_event_at < ?
                    """,
                    (projection_cutoff,),
                )
            ]
            old_sessions = [
                str(row["session_id"])
                for row in connection.execute(
                    """
                    SELECT s.session_id FROM sessions AS s
                    JOIN projection_outbox AS o
                      ON o.entity_type='session' AND o.entity_id=s.session_id
                    WHERE o.status='synced' AND s.last_event_at != ''
                      AND s.last_event_at < ?
                    """,
                    (projection_cutoff,),
                )
            ]
            for entity_type, identifiers in (
                ("playback", old_playbacks),
                ("session", old_sessions),
            ):
                if not identifiers:
                    continue
                placeholders = ",".join("?" for _ in identifiers)
                connection.execute(
                    f"DELETE FROM projection_outbox WHERE entity_type=? "
                    f"AND entity_id IN ({placeholders})",
                    (entity_type, *identifiers),
                )
            if old_playbacks:
                placeholders = ",".join("?" for _ in old_playbacks)
                connection.execute(
                    f"DELETE FROM playbacks WHERE playback_id IN ({placeholders})",
                    old_playbacks,
                )
            if old_sessions:
                placeholders = ",".join("?" for _ in old_sessions)
                connection.execute(
                    f"DELETE FROM sessions WHERE session_id IN ({placeholders})",
                    old_sessions,
                )
            deleted_events = connection.execute(
                """
                DELETE FROM events WHERE occurred_at < ? AND event_id IN (
                    SELECT event_id FROM sync_outbox WHERE status='synced'
                )
                """,
                (raw_cutoff,),
            ).rowcount
        return {
            "events": int(deleted_events),
            "playbacks": len(old_playbacks),
            "sessions": len(old_sessions),
        }

    def claim_projection_batch(
        self, limit: int = 20, *, stale_after_seconds: float = 300
    ) -> list[ProjectionOutboxItem]:
        bounded_limit = max(1, min(limit, 100))
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE projection_outbox SET status='pending', updated_at=?
                WHERE status='sending' AND updated_at <= ?
                """,
                (now, now - stale_after_seconds),
            )
            outbox_rows = connection.execute(
                """
                SELECT * FROM projection_outbox
                WHERE status='pending' AND next_attempt_at <= ?
                ORDER BY updated_at, entity_type, entity_id LIMIT ?
                """,
                (now, bounded_limit),
            ).fetchall()
            items: list[ProjectionOutboxItem] = []
            for outbox in outbox_rows:
                table = "playbacks" if outbox["entity_type"] == "playback" else "sessions"
                key = "playback_id" if table == "playbacks" else "session_id"
                row = connection.execute(
                    f"SELECT * FROM {table} WHERE {key} = ?", (outbox["entity_id"],)
                ).fetchone()
                if row is None:
                    connection.execute(
                        "DELETE FROM projection_outbox WHERE entity_type=? AND entity_id=?",
                        (outbox["entity_type"], outbox["entity_id"]),
                    )
                    continue
                items.append(
                    ProjectionOutboxItem(
                        entity_type=str(outbox["entity_type"]),
                        entity_id=str(outbox["entity_id"]),
                        revision=int(outbox["revision"]),
                        remote_record_id=str(outbox["remote_record_id"]),
                        fields=dict(row),
                        attempts=int(outbox["attempts"]),
                    )
                )
            if items:
                connection.executemany(
                    """
                    UPDATE projection_outbox SET status='sending', updated_at=?
                    WHERE entity_type=? AND entity_id=?
                    """,
                    [(now, item.entity_type, item.entity_id) for item in items],
                )
        return items

    def mark_projection_synced(
        self, entity_type: str, entity_id: str, revision: int, remote_record_id: str
    ) -> None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT revision FROM projection_outbox WHERE entity_type=? AND entity_id=?",
                (entity_type, entity_id),
            ).fetchone()
            if row is None:
                return
            if int(row["revision"]) == revision:
                connection.execute(
                    """
                    UPDATE projection_outbox SET status='synced', remote_record_id=?,
                        last_error='', updated_at=? WHERE entity_type=? AND entity_id=?
                    """,
                    (remote_record_id[:128], time.time(), entity_type, entity_id),
                )
            elif remote_record_id:
                connection.execute(
                    """
                    UPDATE projection_outbox SET remote_record_id=?, status='pending',
                        next_attempt_at=0, updated_at=? WHERE entity_type=? AND entity_id=?
                    """,
                    (remote_record_id[:128], time.time(), entity_type, entity_id),
                )

    def release_projection_batch(
        self, items: list[ProjectionOutboxItem], error: str, *, delay_seconds: float = 30
    ) -> None:
        now = time.time()
        with self._connection() as connection:
            connection.executemany(
                """
                UPDATE projection_outbox SET status='pending', next_attempt_at=?,
                    last_error=?, updated_at=? WHERE entity_type=? AND entity_id=?
                """,
                [
                    (
                        now + max(0, delay_seconds),
                        mask_text(error)[:1000],
                        now,
                        item.entity_type,
                        item.entity_id,
                    )
                    for item in items
                ],
            )

    def mark_projection_failed(
        self, item: ProjectionOutboxItem, error: str, *, max_attempts: int = 8
    ) -> str:
        attempts = item.attempts + 1
        status = "dead" if attempts >= max_attempts else "pending"
        delay = 0 if status == "dead" else min(3600, 2 ** min(attempts, 10))
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE projection_outbox SET status=?, attempts=?, next_attempt_at=?,
                    last_error=?, updated_at=? WHERE entity_type=? AND entity_id=?
                """,
                (
                    status,
                    attempts,
                    time.time() + delay,
                    mask_text(error)[:1000],
                    time.time(),
                    item.entity_type,
                    item.entity_id,
                ),
            )
        return status

    def rebuild_playback_projections(self) -> int:
        with self._connection() as connection:
            connection.execute("DELETE FROM projection_outbox")
            connection.execute("DELETE FROM playbacks")
            connection.execute("DELETE FROM sessions")
            rows = connection.execute(
                "SELECT * FROM events ORDER BY created_at, event_id"
            ).fetchall()
            rebuilt = set()
            now = time.time()
            for row in rows:
                event = self._event_from_row(row)
                playback_id = str((event.payload or {}).get("playback_id", ""))
                if event.event_type in PLAYBACK_EVENT_TYPES and playback_id:
                    self._apply_playback_projection(connection, event, now)
                    rebuilt.add(playback_id)
                self._apply_session_projection(connection, event, now)
                self._refresh_search_dissatisfaction(connection, event, now)
        return len(rebuilt)

    def claim_batch(self, limit: int = 100, *, stale_after_seconds: float = 300) -> list[OutboxItem]:
        bounded_limit = max(1, min(limit, 500))
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE sync_outbox
                SET status = 'pending', updated_at = ?
                WHERE status = 'sending' AND updated_at <= ?
                """,
                (now, now - stale_after_seconds),
            )
            rows = connection.execute(
                """
                SELECT e.*, o.attempts
                FROM sync_outbox AS o
                JOIN events AS e ON e.event_id = o.event_id
                WHERE o.status = 'pending' AND o.next_attempt_at <= ?
                ORDER BY e.created_at, e.event_id
                LIMIT ?
                """,
                (now, bounded_limit),
            ).fetchall()
            event_ids = [row["event_id"] for row in rows]
            if event_ids:
                placeholders = ",".join("?" for _ in event_ids)
                connection.execute(
                    f"UPDATE sync_outbox SET status = 'sending', updated_at = ? "
                    f"WHERE event_id IN ({placeholders})",
                    (now, *event_ids),
                )
        return [OutboxItem(self._event_from_row(row), row["attempts"]) for row in rows]

    def mark_synced(self, event_id: str, remote_record_id: str = "") -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE sync_outbox
                SET status = 'synced', remote_record_id = ?, last_error = '', updated_at = ?
                WHERE event_id = ?
                """,
                (remote_record_id[:128], time.time(), event_id),
            )

    def mark_batch_synced(self, remote_record_ids: Mapping[str, str]) -> None:
        now = time.time()
        with self._connection() as connection:
            connection.executemany(
                """
                UPDATE sync_outbox
                SET status = 'synced', remote_record_id = ?, last_error = '', updated_at = ?
                WHERE event_id = ?
                """,
                [
                    (remote_id[:128], now, event_id)
                    for event_id, remote_id in remote_record_ids.items()
                ],
            )

    def mark_failed(self, event_id: str, error: str, *, max_attempts: int = 8) -> str:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT attempts FROM sync_outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            attempts = int(row["attempts"]) + 1
            status = "dead" if attempts >= max_attempts else "pending"
            delay = 0 if status == "dead" else min(3600, 2 ** min(attempts, 10))
            connection.execute(
                """
                UPDATE sync_outbox
                SET status = ?, attempts = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (status, attempts, time.time() + delay, mask_text(error)[:1000], time.time(), event_id),
            )
        return status

    def release_batch(self, event_ids: list[str], error: str, *, delay_seconds: float = 30) -> None:
        if not event_ids:
            return
        now = time.time()
        with self._connection() as connection:
            connection.executemany(
                """
                UPDATE sync_outbox
                SET status = 'pending', next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE event_id = ?
                """,
                [
                    (now + max(0, delay_seconds), mask_text(error)[:1000], now, event_id)
                    for event_id in event_ids
                ],
            )

    def retry_dead(self) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE sync_outbox
                SET status = 'pending', attempts = 0, next_attempt_at = 0,
                    last_error = '', updated_at = ?
                WHERE status = 'dead'
                """,
                (time.time(),),
            )
            projection_cursor = connection.execute(
                """
                UPDATE projection_outbox
                SET status='pending', attempts=0, next_attempt_at=0,
                    last_error='', updated_at=? WHERE status='dead'
                """,
                (time.time(),),
            )
        return cursor.rowcount + projection_cursor.rowcount

    def status(self) -> dict[str, int]:
        result = {"pending": 0, "sending": 0, "synced": 0, "dead": 0, "events": 0}
        with self._connection() as connection:
            result["events"] = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM sync_outbox GROUP BY status"
            ):
                result[str(row["status"])] = int(row["count"])
        return result

    def event_sync_status(self, event_id: str) -> str:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT status FROM sync_outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
        return str(row["status"]) if row is not None else "missing"

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> AnalyticsEvent:
        return AnalyticsEvent(
            event_id=row["event_id"],
            event_type=row["event_type"],
            occurred_at=row["occurred_at"],
            received_at=row["received_at"],
            source=row["source"],
            device_id=row["device_id"],
            session_id=row["session_id"],
            trace_id=row["trace_id"],
            payload=json.loads(row["payload_json"]),
            schema_version=row["schema_version"],
        )


class AnalyticsRecorder:
    """Best-effort recorder that never raises into the music path."""

    def __init__(self, store: AnalyticsStore, *, privacy_mode: str = "masked", device_salt: str = "xiaozhi-local"):
        self.store = store
        self.privacy_mode = privacy_mode
        self.device_salt = device_salt

    def emit(
        self,
        event_type: str,
        *,
        source: str,
        payload: Mapping[str, Any] | None = None,
        device_id: str = "",
        session_id: str = "",
        trace_id: str = "",
        event_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> str:
        event = self.create_event(
            event_type,
            source=source,
            payload=payload,
            device_id=device_id,
            session_id=session_id,
            trace_id=trace_id,
            event_id=event_id,
            occurred_at=occurred_at,
        )
        try:
            self.store.append(event)
        except Exception as exc:  # analytics must never break music playback
            LOGGER.warning("无法写入使用行为事件 %s：%s", event_type, mask_text(str(exc)))
        return event.event_id

    def create_event(
        self,
        event_type: str,
        *,
        source: str,
        payload: Mapping[str, Any] | None = None,
        device_id: str = "",
        session_id: str = "",
        trace_id: str = "",
        event_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> AnalyticsEvent:
        return AnalyticsEvent.create(
            event_type,
            source=source,
            payload=payload,
            device_id=device_id,
            session_id=session_id,
            trace_id=trace_id,
            event_id=event_id,
            occurred_at=occurred_at,
            privacy_mode=self.privacy_mode,
            device_salt=self.device_salt,
        )


_RECORDER: AnalyticsRecorder | None = None
_RECORDER_LOCK = threading.Lock()
_RECORDER_INITIALIZATION_FAILED = False


def get_recorder(environ: Mapping[str, str] | None = None) -> AnalyticsRecorder | None:
    global _RECORDER, _RECORDER_INITIALIZATION_FAILED
    source = environ or os.environ
    if not analytics_enabled(source) or _RECORDER_INITIALIZATION_FAILED:
        return None
    if _RECORDER is None:
        with _RECORDER_LOCK:
            if _RECORDER is None:
                try:
                    salt = source.get("ANALYTICS_DEVICE_SALT", "xiaozhi-local").strip() or "xiaozhi-local"
                    _RECORDER = AnalyticsRecorder(
                        AnalyticsStore(default_database_path(source)),
                        privacy_mode=transcript_mode(source),
                        device_salt=salt,
                    )
                except Exception as exc:
                    _RECORDER_INITIALIZATION_FAILED = True
                    LOGGER.warning("无法初始化使用行为数据库，统计已降级：%s", mask_text(str(exc)))
    return _RECORDER
