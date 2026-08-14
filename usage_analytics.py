#!/usr/bin/env python3
"""Local-first usage analytics primitives for the music MCP service."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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
                """
            )

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
        return True

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
        return cursor.rowcount

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
    ) -> str:
        event = AnalyticsEvent.create(
            event_type,
            source=source,
            payload=payload,
            device_id=device_id,
            session_id=session_id,
            trace_id=trace_id,
            event_id=event_id,
            privacy_mode=self.privacy_mode,
            device_salt=self.device_salt,
        )
        try:
            self.store.append(event)
        except Exception as exc:  # analytics must never break music playback
            LOGGER.warning("无法写入使用行为事件 %s：%s", event_type, mask_text(str(exc)))
        return event.event_id


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
