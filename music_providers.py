#!/usr/bin/env python3
"""Music provider implementations and priority-based orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import os
import secrets
import ssl
from typing import Any, Protocol
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("xiaozhi-music-providers")
DEFAULT_TIMEOUT_SECONDS = 5.0


class ProviderError(RuntimeError):
    """Raised when a configured provider cannot complete a search."""


@dataclass(frozen=True, slots=True)
class Track:
    provider: str
    track_id: str
    title: str
    artist: str
    audio_url: str
    album: str = ""
    duration: int | None = None
    content_type: str = "audio/mpeg"
    artwork_url: str = ""
    is_preview: bool = False

    def public_dict(self) -> dict[str, Any]:
        """Return metadata safe to expose to the assistant and logs."""
        value = asdict(self)
        value.pop("audio_url", None)
        return value


class MusicProvider(Protocol):
    name: str

    async def search(self, query: str, limit: int = 5) -> list[Track]: ...


def _verified_ssl_context() -> ssl.SSLContext:
    """Keep CA/hostname checks while relaxing Python 3.13's extra chain strictness."""
    context = ssl.create_default_context()
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def _get_json(url: str, timeout: float, headers: dict[str, str] | None = None) -> Any:
    request_headers = {"Accept": "application/json", "User-Agent": "xiaozhi-music-mcp/2.0"}
    request_headers.update(headers or {})
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout, context=_verified_ssl_context()) as response:
            return json.load(response)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ProviderError(str(exc)) from exc


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value)))
    except ValueError:
        return None


def _timeout_from_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip())
    except ValueError:
        return default
    return value if 1 <= value <= 60 else default


class NavidromeProvider:
    """Search and stream a Navidrome/OpenSubsonic library."""

    name = "navidrome"

    def __init__(self, base_url: str, username: str, password: str, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.base_url = base_url.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.timeout = timeout

    def _auth_params(self) -> dict[str, str]:
        salt = secrets.token_hex(6)
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()  # noqa: S324 - Subsonic protocol
        return {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "xiaozhi-music-mcp",
            "f": "json",
        }

    def _api_url(self, method: str, **params: object) -> str:
        query = self._auth_params()
        query.update({key: str(value) for key, value in params.items()})
        return urljoin(self.base_url, f"rest/{method}") + "?" + urlencode(query)

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        url = self._api_url(
            "search3",
            query=query,
            songCount=max(1, min(limit, 20)),
            albumCount=0,
            artistCount=0,
        )
        payload = await asyncio.to_thread(_get_json, url, self.timeout)
        root = payload.get("subsonic-response", {}) if isinstance(payload, dict) else {}
        if root.get("status") == "failed":
            error = root.get("error", {})
            raise ProviderError(error.get("message", "Navidrome request failed"))
        songs = root.get("searchResult3", {}).get("song", [])
        tracks: list[Track] = []
        for song in songs if isinstance(songs, list) else []:
            track_id = str(song.get("id", ""))
            title = str(song.get("title", "")).strip()
            if not track_id or not title:
                continue
            tracks.append(
                Track(
                    provider=self.name,
                    track_id=track_id,
                    title=title,
                    artist=str(song.get("artist", "未知歌手")),
                    album=str(song.get("album", "")),
                    duration=_optional_int(song.get("duration")),
                    content_type="audio/mpeg",
                    artwork_url="",
                    audio_url=self._api_url("stream", id=track_id, format="mp3", maxBitRate=192),
                )
            )
        return tracks


class JamendoProvider:
    """Search Jamendo's official public catalogue."""

    name = "jamendo"

    def __init__(self, client_id: str, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.client_id = client_id
        self.timeout = timeout

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        params = {
            "client_id": self.client_id,
            "format": "json",
            "limit": max(1, min(limit, 20)),
            "search": query,
            "audioformat": "mp32",
            "order": "relevance",
            "track_type": "single albumtrack",
        }
        url = "https://api.jamendo.com/v3.0/tracks/?" + urlencode(params)
        payload = await asyncio.to_thread(_get_json, url, self.timeout)
        headers = payload.get("headers", {}) if isinstance(payload, dict) else {}
        if headers.get("status") != "success":
            raise ProviderError(headers.get("error_message", "Jamendo request failed"))
        tracks: list[Track] = []
        for item in payload.get("results", []):
            audio_url = str(item.get("audio", "")).strip()
            title = str(item.get("name", "")).strip()
            if not audio_url or not title:
                continue
            tracks.append(
                Track(
                    provider=self.name,
                    track_id=str(item.get("id", "")),
                    title=title,
                    artist=str(item.get("artist_name", "未知歌手")),
                    album=str(item.get("album_name", "")),
                    duration=_optional_int(item.get("duration")),
                    content_type="audio/mpeg",
                    artwork_url=str(item.get("image", "")),
                    audio_url=audio_url,
                )
            )
        return tracks


class NeteaseProvider:
    """Search a self-hosted NetEase API and return native playable URLs.

    This client never requests the API's ``unblock`` modes. Tracks without a
    platform-provided URL are treated as unavailable. Official preview URLs are
    accepted and marked in public metadata.
    """

    name = "netease"

    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout

    def _api_url(self, path: str, **params: object) -> str:
        return urljoin(self.base_url, path.lstrip("/")) + "?" + urlencode(
            {key: str(value) for key, value in params.items()}
        )

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        result_limit = max(1, min(limit, 10))
        search_url = self._api_url(
            "cloudsearch",
            keywords=query,
            type=1,
            limit=result_limit,
        )
        payload = await asyncio.to_thread(_get_json, search_url, self.timeout)
        if not isinstance(payload, dict) or payload.get("code") != 200:
            raise ProviderError("NetEase search request failed")
        songs = payload.get("result", {}).get("songs", [])
        if not isinstance(songs, list):
            raise ProviderError("NetEase search returned an invalid payload")

        tracks: list[Track] = []
        for song in songs[:result_limit]:
            if not isinstance(song, dict):
                continue
            track_id = str(song.get("id", "")).strip()
            title = str(song.get("name", "")).strip()
            if not track_id or not title:
                continue

            stream_url = self._api_url(
                "song/url/v1",
                id=track_id,
                level="standard",
            )
            stream_payload = await asyncio.to_thread(_get_json, stream_url, self.timeout)
            stream_items = stream_payload.get("data", []) if isinstance(stream_payload, dict) else []
            stream = stream_items[0] if isinstance(stream_items, list) and stream_items else {}
            if not isinstance(stream, dict):
                continue
            audio_url = str(stream.get("url") or "").strip()
            if not audio_url or urlsplit(audio_url).scheme not in {"http", "https"}:
                continue

            artists = song.get("ar", [])
            artist = "/".join(
                str(item.get("name", "")).strip()
                for item in artists
                if isinstance(item, dict) and item.get("name")
            )
            album = song.get("al", {})
            audio_type = str(stream.get("type", "mp3")).lower()
            duration_ms = _optional_int(song.get("dt"))
            tracks.append(
                Track(
                    provider=self.name,
                    track_id=track_id,
                    title=title,
                    artist=artist or "未知歌手",
                    album=str(album.get("name", "")) if isinstance(album, dict) else "",
                    duration=duration_ms // 1000 if duration_ms else None,
                    content_type="audio/flac" if audio_type == "flac" else "audio/mpeg",
                    artwork_url=str(album.get("picUrl", "")) if isinstance(album, dict) else "",
                    audio_url=audio_url,
                    is_preview=stream.get("freeTrialInfo") is not None,
                )
            )
            # ProviderChain consumes only the first playable result. Returning
            # immediately avoids resolving up to nine unused stream URLs and
            # keeps voice requests comfortably inside the provider timeout.
            return tracks
        return tracks


class HttpJsonProvider:
    """Adapter for an optional user-managed unofficial aggregation service.

    The endpoint receives GET parameters ``q`` and ``limit`` and must return
    either a JSON list or ``{"tracks": [...]}``. Each item needs ``id``,
    ``title``, ``artist`` and ``audio_url`` fields.
    """

    name = "unofficial"

    def __init__(self, endpoint: str, token: str = "", timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.endpoint = endpoint
        self.token = token
        self.timeout = timeout

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        separator = "&" if "?" in self.endpoint else "?"
        url = self.endpoint + separator + urlencode({"q": query, "limit": limit})
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        payload = await asyncio.to_thread(_get_json, url, self.timeout, headers)
        items = payload.get("tracks", []) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise ProviderError("unofficial provider returned an invalid payload")
        tracks: list[Track] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            audio_url = str(item.get("audio_url", "")).strip()
            title = str(item.get("title", "")).strip()
            if not audio_url or not title or urlsplit(audio_url).scheme not in {"http", "https"}:
                continue
            tracks.append(
                Track(
                    provider=self.name,
                    track_id=str(item.get("id", "")),
                    title=title,
                    artist=str(item.get("artist", "未知歌手")),
                    album=str(item.get("album", "")),
                    duration=_optional_int(item.get("duration")),
                    content_type=str(item.get("content_type", "audio/mpeg")),
                    artwork_url=str(item.get("artwork_url", "")),
                    audio_url=audio_url,
                )
            )
        return tracks


class ProviderChain:
    """Search providers sequentially and stop at the first useful result."""

    def __init__(self, providers: list[MusicProvider], timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.providers = providers
        self.timeout = timeout

    async def search(self, query: str, limit: int = 5) -> tuple[Track | None, list[dict[str, str]]]:
        failures: list[dict[str, str]] = []
        for provider in self.providers:
            try:
                tracks = await asyncio.wait_for(provider.search(query, limit), timeout=self.timeout)
            except asyncio.TimeoutError:
                LOGGER.warning(
                    "音乐源 %s 搜索超时（query=%r timeout=%.1fs）",
                    provider.name,
                    query,
                    self.timeout,
                )
                failures.append({"provider": provider.name, "reason": "timeout"})
                continue
            except (ProviderError, OSError) as exc:
                LOGGER.warning("音乐源 %s 搜索失败：%s", provider.name, exc)
                failures.append({"provider": provider.name, "reason": str(exc)})
                continue
            if tracks:
                return tracks[0], failures
        return None, failures


def providers_from_env() -> list[MusicProvider]:
    """Build configured providers in the requested priority order."""
    available: dict[str, MusicProvider] = {}
    navidrome_url = os.getenv("NAVIDROME_URL", "").strip()
    navidrome_username = os.getenv("NAVIDROME_USERNAME", "").strip()
    navidrome_password = os.getenv("NAVIDROME_PASSWORD", "")
    if navidrome_url and navidrome_username and navidrome_password:
        available["navidrome"] = NavidromeProvider(
            navidrome_url,
            navidrome_username,
            navidrome_password,
        )
    if client_id := os.getenv("JAMENDO_CLIENT_ID", "").strip():
        available["jamendo"] = JamendoProvider(client_id)
    if os.getenv("NETEASE_PROVIDER_ENABLED", "false").strip().lower() in {"1", "true", "yes"}:
        if endpoint := os.getenv("NETEASE_API_URL", "").strip():
            available["netease"] = NeteaseProvider(
                endpoint,
                timeout=_timeout_from_env("NETEASE_API_TIMEOUT_SECONDS", 12.0),
            )
    if os.getenv("UNOFFICIAL_PROVIDER_ENABLED", "false").strip().lower() in {"1", "true", "yes"}:
        if endpoint := os.getenv("UNOFFICIAL_PROVIDER_URL", "").strip():
            available["unofficial"] = HttpJsonProvider(
                endpoint,
                os.getenv("UNOFFICIAL_PROVIDER_TOKEN", "").strip(),
            )

    requested = os.getenv("MUSIC_PROVIDER_ORDER", "navidrome,netease,jamendo,unofficial")
    order = [item.strip().lower() for item in requested.split(",") if item.strip()]
    return [available[name] for name in order if name in available]
