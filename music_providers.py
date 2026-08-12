#!/usr/bin/env python3
"""Music provider implementations and priority-based orchestration."""

from __future__ import annotations

import asyncio
import ast
from dataclasses import asdict, dataclass
import hashlib
from html import unescape
import json
import logging
import os
import re
import secrets
import ssl
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from curl_cffi import requests as curl_requests


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
    lyrics: str = ""
    lyrics_url: str = ""
    is_preview: bool = False

    def public_dict(self) -> dict[str, Any]:
        """Return metadata safe to expose to the assistant and logs."""
        value = asdict(self)
        value.pop("audio_url", None)
        value.pop("lyrics", None)
        value.pop("lyrics_url", None)
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
                    artwork_url=self._api_url("getCoverArt", id=str(song.get("coverArt", "")))
                    if song.get("coverArt")
                    else "",
                    lyrics=await self._lyrics(track_id),
                    audio_url=self._api_url("stream", id=track_id, format="mp3", maxBitRate=192),
                )
            )
            return tracks
        return tracks

    async def _lyrics(self, track_id: str) -> str:
        """Return OpenSubsonic lyrics when the server implements the endpoint."""
        try:
            payload = await asyncio.to_thread(
                _get_json,
                self._api_url("getLyricsBySongId", id=track_id),
                self.timeout,
            )
        except ProviderError:
            return ""
        root = payload.get("subsonic-response", {}) if isinstance(payload, dict) else {}
        structured = root.get("lyricsList", {}).get("structuredLyrics", [])
        if not isinstance(structured, list) or not structured:
            return ""
        lines = structured[0].get("line", []) if isinstance(structured[0], dict) else []
        rendered: list[str] = []
        for line in lines if isinstance(lines, list) else []:
            if not isinstance(line, dict):
                continue
            text = str(line.get("value", "")).strip()
            start = _optional_int(line.get("start"))
            if not text or start is None:
                continue
            minutes, remainder = divmod(start, 60_000)
            seconds, millis = divmod(remainder, 1000)
            rendered.append(f"[{minutes:02d}:{seconds:02d}.{millis // 10:02d}]{text}")
        return "\n".join(rendered)


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

            duration_ms = _optional_int(song.get("dt"))
            stream_duration_ms = _optional_int(stream.get("time"))
            has_trial_metadata = stream.get("freeTrialInfo") is not None or stream.get("trialInfo") is not None
            has_truncated_duration = bool(
                duration_ms
                and stream_duration_ms
                and stream_duration_ms + 1000 < duration_ms
            )
            if has_trial_metadata or has_truncated_duration:
                LOGGER.info("跳过网易云试听歌曲：%s (id=%s)", title, track_id)
                continue

            artists = song.get("ar", [])
            artist = "/".join(
                str(item.get("name", "")).strip()
                for item in artists
                if isinstance(item, dict) and item.get("name")
            )
            album = song.get("al", {})
            audio_type = str(stream.get("type", "mp3")).lower()
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
                    lyrics=await self._lyrics(track_id),
                    audio_url=audio_url,
                )
            )
            # ProviderChain consumes only the first playable result. Returning
            # immediately avoids resolving up to nine unused stream URLs and
            # keeps voice requests comfortably inside the provider timeout.
            return tracks
        return tracks

    async def _lyrics(self, track_id: str) -> str:
        try:
            payload = await asyncio.to_thread(
                _get_json,
                self._api_url("lyric", id=track_id),
                self.timeout,
            )
        except ProviderError:
            return ""
        if not isinstance(payload, dict):
            return ""
        lyric = payload.get("lrc", {}).get("lyric", "")
        return str(lyric) if lyric else ""


def _parse_fangpi_search_results(html: str, base_url: str = "https://www.fangpi.net") -> list[dict[str, str]]:
    """Extract unique public track pages from Fangpi's server-rendered search page."""
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    pattern = re.compile(
        r'<a\b(?=[^>]*\bhref=["\'](?P<href>/music/\d+)["\'])(?=[^>]*\btitle=["\'](?P<title>.*?)["\'])[^>]*>',
        re.IGNORECASE,
    )
    for match in pattern.finditer(html):
        track_url = urljoin(base_url, unescape(match.group("href")))
        if track_url in seen:
            continue
        seen.add(track_url)
        label = unescape(match.group("title")).strip()
        title, separator, artist = label.rpartition(" - ")
        if not separator:
            title, artist = label, "未知歌手"
        results.append(
            {
                "id": track_url.rstrip("/").rsplit("/", 1)[-1],
                "title": title.strip(),
                "artist": artist.strip() or "未知歌手",
                "url": track_url,
            }
        )
    return results


def _parse_fangpi_app_data(html: str) -> dict[str, Any]:
    """Decode the JSON metadata embedded in a Fangpi track page."""
    match = re.search(r"window\.appData\s*=\s*JSON\.parse\('((?:\\.|[^'])*)'\)", html, re.DOTALL)
    if not match:
        raise ProviderError("Fangpi track page did not contain appData")
    try:
        decoded = ast.literal_eval("'" + match.group(1) + "'")
        payload = json.loads(decoded)
    except (SyntaxError, ValueError, json.JSONDecodeError) as exc:
        raise ProviderError("Fangpi track metadata was invalid") from exc
    if not isinstance(payload, dict):
        raise ProviderError("Fangpi track metadata was invalid")
    return payload


class FangpiProvider:
    """Resolve public Fangpi search results to short-lived audio URLs."""

    name = "fangpi"
    base_url = "https://www.fangpi.net"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS, cookie: str = "", user_agent: str = ""):
        self.timeout = timeout
        self.session = curl_requests.Session(impersonate="chrome")
        self.headers = {
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Referer": self.base_url + "/",
        }
        if cookie:
            self.headers["Cookie"] = cookie
        if user_agent:
            self.headers["User-Agent"] = user_agent

    def _get_text(self, url: str) -> str:
        try:
            response = self.session.get(url, headers=self.headers, timeout=self.timeout)
            response.raise_for_status()
            return response.text
        except curl_requests.RequestsError as exc:
            raise ProviderError(str(exc)) from exc

    def _post_json(self, url: str, data: dict[str, str]) -> Any:
        try:
            response = self.session.post(
                url,
                data=data,
                headers={**self.headers, "X-Requested-With": "XMLHttpRequest"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except (curl_requests.RequestsError, ValueError) as exc:
            raise ProviderError(str(exc)) from exc

    def _search_sync(self, query: str, limit: int) -> list[Track]:
        search_url = self.base_url + "/s/" + quote(query, safe="")
        results = _parse_fangpi_search_results(self._get_text(search_url), self.base_url)
        for result in results[: max(1, min(limit, 5))]:
            try:
                metadata = _parse_fangpi_app_data(self._get_text(result["url"]))
                play_id = str(metadata.get("play_id") or "").strip()
                if not play_id:
                    continue
                payload = self._post_json(
                    self.base_url + "/member/common-play-url",
                    {"id": play_id},
                )
                data = payload.get("data", {}) if isinstance(payload, dict) else {}
                audio_url = str(data.get("url") or "").replace("\\/", "/").strip()
                if urlsplit(audio_url).scheme not in {"http", "https"}:
                    continue
                duration_text = str(metadata.get("mp3_duration") or "")
                duration_parts = [int(value) for value in re.findall(r"\d+", duration_text)]
                duration = None
                if len(duration_parts) == 2:
                    duration = duration_parts[0] * 60 + duration_parts[1]
                elif len(duration_parts) == 3:
                    duration = duration_parts[0] * 3600 + duration_parts[1] * 60 + duration_parts[2]
                return [
                    Track(
                        provider=self.name,
                        track_id=str(metadata.get("mp3_id") or result["id"]),
                        title=str(metadata.get("mp3_title") or result["title"]).strip(),
                        artist=str(metadata.get("mp3_author") or result["artist"]).strip(),
                        duration=duration,
                        content_type="audio/mpeg",
                        artwork_url=str(metadata.get("mp3_cover") or "").replace("\\/", "/"),
                        lyrics=str(metadata.get("mp3_lyric") or metadata.get("lyric") or ""),
                        audio_url=audio_url,
                    )
                ]
            except ProviderError as exc:
                LOGGER.info("Fangpi 候选歌曲解析失败：%s", exc)
        return []

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        return await asyncio.to_thread(self._search_sync, query, limit)


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
                    lyrics=str(item.get("lyrics") or item.get("lyric") or ""),
                    lyrics_url=str(item.get("lyrics_url") or item.get("lyric_url") or ""),
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
    if os.getenv("FANGPI_PROVIDER_ENABLED", "true").strip().lower() in {"1", "true", "yes"}:
        available["fangpi"] = FangpiProvider(
            timeout=_timeout_from_env("FANGPI_API_TIMEOUT_SECONDS", 10.0),
            cookie=os.getenv("FANGPI_COOKIE", "").strip(),
            user_agent=os.getenv("FANGPI_USER_AGENT", "").strip(),
        )
    if os.getenv("UNOFFICIAL_PROVIDER_ENABLED", "false").strip().lower() in {"1", "true", "yes"}:
        if endpoint := os.getenv("UNOFFICIAL_PROVIDER_URL", "").strip():
            available["unofficial"] = HttpJsonProvider(
                endpoint,
                os.getenv("UNOFFICIAL_PROVIDER_TOKEN", "").strip(),
            )

    requested = os.getenv("MUSIC_PROVIDER_ORDER", "navidrome,netease,fangpi,jamendo,unofficial")
    order = [item.strip().lower() for item in requested.split(",") if item.strip()]
    return [available[name] for name in order if name in available]
