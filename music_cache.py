#!/usr/bin/env python3
"""Persistent, directly playable MP3 cache for the music proxy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from typing import Any, BinaryIO, Mapping


DEFAULT_CACHE_BYTES = 50 * 1024**3
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def normalize_cache_query(value: str) -> str:
    """Normalize an exact query alias without applying fuzzy version matching."""
    return re.sub(r"[^\w]+", "", value.casefold(), flags=re.UNICODE)


def _safe_filename_part(value: str, fallback: str) -> str:
    cleaned = _INVALID_FILENAME.sub("_", value).strip(" ._")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or fallback)[:80]


def _looks_like_mp3(path: Path) -> bool:
    with path.open("rb") as source:
        header = source.read(64 * 1024)
    if header.startswith(b"ID3"):
        return True
    return any(
        header[index] == 0xFF and header[index + 1] & 0xE0 == 0xE0
        for index in range(max(0, len(header) - 1))
    )


@dataclass(frozen=True, slots=True)
class CachedTrack:
    cache_id: str
    provider: str
    track_id: str
    title: str
    artist: str
    album: str
    audio_path: str
    content_type: str
    size_bytes: int
    duration_ms: int | None
    song_duration_ms: int | None
    artwork_url: str
    lyrics: str
    lyrics_url: str
    created_at: float
    last_accessed: float
    hit_count: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CacheCommitResult:
    track: CachedTrack
    evicted: tuple[CachedTrack, ...]


class MusicCache:
    """SQLite-indexed MP3 files with atomic commits and LRU eviction."""

    def __init__(self, directory: Path | str, max_bytes: int = DEFAULT_CACHE_BYTES):
        self.directory = Path(directory).expanduser()
        self.max_bytes = max(0, int(max_bytes))
        self.database_path = self.directory / "index.sqlite3"
        self._schema_lock = threading.Lock()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def enabled(self) -> bool:
        return self.max_bytes > 0

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
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
                CREATE TABLE IF NOT EXISTS tracks (
                    cache_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    track_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    album TEXT NOT NULL DEFAULT '',
                    filename TEXT NOT NULL UNIQUE,
                    content_type TEXT NOT NULL DEFAULT 'audio/mpeg',
                    size_bytes INTEGER NOT NULL,
                    duration_ms INTEGER,
                    song_duration_ms INTEGER,
                    artwork_url TEXT NOT NULL DEFAULT '',
                    lyrics TEXT NOT NULL DEFAULT '',
                    lyrics_url TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    last_accessed REAL NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_cache_tracks_accessed
                    ON tracks(last_accessed);
                CREATE TABLE IF NOT EXISTS aliases (
                    alias TEXT NOT NULL,
                    cache_id TEXT NOT NULL REFERENCES tracks(cache_id) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(alias, cache_id)
                );
                CREATE INDEX IF NOT EXISTS idx_cache_aliases_alias
                    ON aliases(alias, created_at DESC);
                """
            )
            connection.execute("PRAGMA foreign_keys = ON")
            missing = [
                row["cache_id"]
                for row in connection.execute("SELECT cache_id, filename FROM tracks")
                if not (self.directory / row["filename"]).is_file()
            ]
            if missing:
                connection.executemany("DELETE FROM tracks WHERE cache_id = ?", ((item,) for item in missing))

    def create_temporary_file(self) -> tuple[BinaryIO, Path]:
        handle = tempfile.NamedTemporaryFile(
            mode="w+b", prefix=".xiaozhi-download-", suffix=".part", dir=self.directory, delete=False
        )
        return handle, Path(handle.name)

    def lookup(self, query: str) -> CachedTrack | None:
        alias = normalize_cache_query(query)
        if not self.enabled or not alias:
            return None
        now = time.time()
        with self._connection() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            row = connection.execute(
                """
                SELECT tracks.* FROM aliases
                JOIN tracks ON tracks.cache_id = aliases.cache_id
                WHERE aliases.alias = ?
                ORDER BY aliases.created_at DESC, tracks.last_accessed DESC
                LIMIT 1
                """,
                (alias,),
            ).fetchone()
            if row is None:
                return None
            path = self.directory / row["filename"]
            if not path.is_file():
                connection.execute("DELETE FROM tracks WHERE cache_id = ?", (row["cache_id"],))
                return None
            connection.execute(
                "UPDATE tracks SET last_accessed = ?, hit_count = hit_count + 1 WHERE cache_id = ?",
                (now, row["cache_id"]),
            )
            updated = dict(row)
            updated["last_accessed"] = now
            updated["hit_count"] = int(updated["hit_count"]) + 1
            return self._row_to_track(updated)

    def commit(
        self,
        temporary_path: Path,
        metadata: Mapping[str, Any],
        queries: list[str],
    ) -> CacheCommitResult | None:
        """Validate and atomically publish a complete MP3 file."""
        if not self.enabled or not temporary_path.is_file():
            temporary_path.unlink(missing_ok=True)
            return None
        size = temporary_path.stat().st_size
        if size <= 0 or size > self.max_bytes or not _looks_like_mp3(temporary_path):
            temporary_path.unlink(missing_ok=True)
            return None

        provider = str(metadata.get("provider", "unknown"))[:100]
        track_id = str(metadata.get("track_id", ""))[:200]
        title = str(metadata.get("title", "未知歌曲"))[:500]
        artist = str(metadata.get("artist", "未知歌手"))[:500]
        identity = f"{provider}\0{track_id}\0{title}\0{artist}".encode("utf-8")
        cache_id = hashlib.sha256(identity).hexdigest()[:24]
        readable = (
            f"{_safe_filename_part(artist, '未知歌手')} - "
            f"{_safe_filename_part(title, '未知歌曲')} [{cache_id[:8]}].mp3"
        )
        destination = self.directory / readable
        os.replace(temporary_path, destination)
        now = time.time()
        aliases = {
            normalize_cache_query(value)
            for value in queries
            if normalize_cache_query(value)
        }
        evicted: list[CachedTrack] = []
        with self._connection() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            previous = connection.execute(
                "SELECT filename, created_at, hit_count FROM tracks WHERE cache_id = ?", (cache_id,)
            ).fetchone()
            created_at = float(previous["created_at"]) if previous else now
            hit_count = int(previous["hit_count"]) if previous else 0
            if previous and previous["filename"] != readable:
                (self.directory / previous["filename"]).unlink(missing_ok=True)
            connection.execute(
                """
                INSERT INTO tracks (
                    cache_id, provider, track_id, title, artist, album, filename,
                    content_type, size_bytes, duration_ms, song_duration_ms,
                    artwork_url, lyrics, lyrics_url, created_at, last_accessed, hit_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_id) DO UPDATE SET
                    provider=excluded.provider, track_id=excluded.track_id,
                    title=excluded.title, artist=excluded.artist, album=excluded.album,
                    filename=excluded.filename, content_type=excluded.content_type,
                    size_bytes=excluded.size_bytes, duration_ms=excluded.duration_ms,
                    song_duration_ms=excluded.song_duration_ms,
                    artwork_url=excluded.artwork_url, lyrics=excluded.lyrics,
                    lyrics_url=excluded.lyrics_url, last_accessed=excluded.last_accessed
                """,
                (
                    cache_id, provider, track_id, title, artist,
                    str(metadata.get("album", ""))[:500], readable, "audio/mpeg", size,
                    self._optional_int(metadata.get("duration_ms")),
                    self._optional_int(metadata.get("song_duration_ms")),
                    str(metadata.get("artwork_url", ""))[:4000],
                    str(metadata.get("lyrics", ""))[:65536],
                    str(metadata.get("lyrics_url", ""))[:4000],
                    created_at, now, hit_count,
                ),
            )
            connection.executemany(
                "INSERT OR REPLACE INTO aliases(alias, cache_id, created_at) VALUES (?, ?, ?)",
                ((alias, cache_id, now) for alias in aliases),
            )
            total = int(connection.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM tracks").fetchone()[0])
            if total > self.max_bytes:
                for row in connection.execute("SELECT * FROM tracks ORDER BY last_accessed ASC"):
                    if total <= self.max_bytes:
                        break
                    if row["cache_id"] == cache_id and len(evicted) == 0:
                        continue
                    track = self._row_to_track(row)
                    (self.directory / row["filename"]).unlink(missing_ok=True)
                    connection.execute("DELETE FROM tracks WHERE cache_id = ?", (row["cache_id"],))
                    total -= int(row["size_bytes"])
                    evicted.append(track)
            current = connection.execute("SELECT * FROM tracks WHERE cache_id = ?", (cache_id,)).fetchone()
        if current is None:
            destination.unlink(missing_ok=True)
            return None
        return CacheCommitResult(self._row_to_track(current), tuple(evicted))

    def status(self) -> dict[str, int | str]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS size FROM tracks"
            ).fetchone()
        return {
            "directory": str(self.directory),
            "track_count": int(row["count"]),
            "size_bytes": int(row["size"]),
            "max_bytes": self.max_bytes,
        }

    def _row_to_track(self, row: Mapping[str, Any]) -> CachedTrack:
        return CachedTrack(
            cache_id=str(row["cache_id"]), provider=str(row["provider"]),
            track_id=str(row["track_id"]), title=str(row["title"]), artist=str(row["artist"]),
            album=str(row["album"]), audio_path=str(self.directory / str(row["filename"])),
            content_type=str(row["content_type"]), size_bytes=int(row["size_bytes"]),
            duration_ms=self._optional_int(row["duration_ms"]),
            song_duration_ms=self._optional_int(row["song_duration_ms"]),
            artwork_url=str(row["artwork_url"]), lyrics=str(row["lyrics"]),
            lyrics_url=str(row["lyrics_url"]), created_at=float(row["created_at"]),
            last_accessed=float(row["last_accessed"]), hit_count=int(row["hit_count"]),
        )

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
