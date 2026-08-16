#!/usr/bin/env python3
"""Resolve music through prioritized providers for EchoEar playback."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Annotated, Any
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastmcp import FastMCP
from pydantic import Field

from music_providers import ProviderChain, Track, providers_from_env
from music_search import MusicSearchResult, SmartMusicSearch
from usage_analytics import get_recorder


mcp = FastMCP("xiaozhi-music-resolver")
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 20.0
OFFICIAL_TEST_TRACK = Track(
    provider="diagnostic",
    track_id="espressif-stereo-44100",
    title="乐鑫官方立体声测试音频",
    artist="Espressif",
    audio_url="https://dl.espressif.com/dl/audio/ff-16b-2c-44100hz.mp3",
)
TEST_ALIASES = (
    "乐鑫官方测试音频",
    "乐鑫测试音频",
    "官方测试音频",
    "测试音频",
    "测试歌曲",
    "espressif test audio",
)


def _normalize(value: str) -> str:
    return "".join(value.casefold().split())


def provider_timeout_seconds() -> float:
    raw_value = os.getenv("MUSIC_PROVIDER_TIMEOUT_SECONDS", "20").strip()
    try:
        value = float(raw_value)
    except ValueError:
        return DEFAULT_PROVIDER_TIMEOUT_SECONDS
    return value if 1 <= value <= 60 else DEFAULT_PROVIDER_TIMEOUT_SECONDS


def smart_search_enabled() -> bool:
    return os.getenv("MUSIC_SMART_SEARCH_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def resolve_diagnostic_track(query: str) -> Track | None:
    normalized = _normalize(query)
    candidates = (OFFICIAL_TEST_TRACK.title, *TEST_ALIASES)
    if any(_normalize(item) in normalized or normalized in _normalize(item) for item in candidates):
        return OFFICIAL_TEST_TRACK
    return None


def _register_proxy_sync(track: Track, trace_id: str = "") -> tuple[str, str]:
    register_url = os.getenv("MUSIC_PROXY_REGISTER_URL", "").strip()
    register_token = os.getenv("MUSIC_PROXY_REGISTER_TOKEN", "").strip()
    if not register_url:
        return track.audio_url, ""

    body = json.dumps(
        {
            "url": track.audio_url,
            "content_type": track.content_type,
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "duration_ms": track.duration * 1000 if track.duration is not None else None,
            "artwork_url": track.artwork_url,
            "lyrics": track.lyrics,
            "lyrics_url": track.lyrics_url,
            "provider": track.provider,
            "trace_id": trace_id,
        }
    ).encode("utf-8")
    request = Request(
        register_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {register_token}",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urlopen(request, timeout=3) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(f"无法登记局域网播放地址：{exc}") from exc
    public_url = str(payload.get("url", "")).strip()
    if not public_url:
        raise RuntimeError("音频代理没有返回播放地址")
    return public_url, str(payload.get("metadata_url", "")).strip()


async def register_proxy(track: Track, trace_id: str = "") -> tuple[str, str]:
    return await asyncio.to_thread(_register_proxy_sync, track, trace_id)


async def record_event(
    event_type: str,
    *,
    trace_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    recorder = get_recorder()
    if recorder is not None:
        await asyncio.to_thread(
            recorder.emit,
            event_type,
            source="mcp",
            trace_id=trace_id,
            payload=payload,
        )


def _success_payload(
    track: Track,
    audio_url: str,
    metadata_url: str,
    failures: list[dict[str, str]],
) -> str:
    device_arguments: dict[str, Any] = {
        "play_type": "url",
        "url": audio_url,
        "url_song_name": f"{track.title} - {track.artist}",
    }
    if metadata_url:
        device_arguments["metadata_url"] = metadata_url
    payload: dict[str, Any] = {
            "success": True,
            **track.public_dict(),
            "audio_url": audio_url,
            "fallback_failures": failures,
            "next_step": "立即调用设备端 MCP 工具 self.online_music.play_music",
            "device_tool": "self.online_music.play_music",
            "metadata_url": metadata_url or None,
            "device_arguments": device_arguments,
        }
    if track.is_preview:
        payload.update(
            {
                "assistant_notice": track.notice or "这首目前只提供30秒试听，我先播放试听版。",
                "recommended_action": "netease_relogin" if track.access_status in {"login_required", "membership_required"} else "try_another_version",
                "account_command": "bash scripts/music_service.sh netease relogin",
                "next_step": "先用一句话简短说明 assistant_notice，然后立即调用设备端 MCP 工具 self.online_music.play_music",
            }
        )
    return json.dumps(payload, ensure_ascii=False)


def _search_diagnostics(result: MusicSearchResult) -> dict[str, Any]:
    selected = result.selected
    return {
        "raw_query": result.query.raw_query,
        "normalized_query": result.query.normalized_query,
        "correction_type": result.query.correction_type,
        "correction_from": result.query.correction_from,
        "query_variants": list(result.query.variants()),
        "candidate_count": result.candidate_count,
        "selected_rank": selected.candidate.source_rank + 1 if selected else None,
        "match_score": selected.score if selected else None,
        "match_reasons": list(selected.reasons) if selected else [],
        "playback_access": result.track.access_status if result.track else "",
        "is_preview": result.track.is_preview if result.track else False,
        "account_status": result.account_status,
        "top_candidates": [candidate.public_dict() for candidate in result.candidates],
        "rejected": list(result.rejected),
    }


@mcp.tool()
async def resolve_music_url(
    query: Annotated[
        str,
        Field(min_length=1, description="歌曲名，可同时包含歌手名，例如：海阔天空 Beyond"),
    ],
) -> str:
    """按本地 Navidrome、受限网易云、Jamendo、可选非官方源的顺序解析歌曲。

    本工具只解析 URL，不播放音频。成功后必须继续调用设备端工具
    `self.online_music.play_music`，并原样使用返回的 `device_arguments`。
    不要改用官方 `play_music`、`search_music` 或 `self.music.play_song`。
    """
    trace_id = uuid.uuid4().hex
    started_at = time.monotonic()
    await record_event(
        "music_search_started",
        trace_id=trace_id,
        payload={"query": query},
    )
    if diagnostic := resolve_diagnostic_track(query):
        audio_url, metadata_url = await register_proxy(diagnostic, trace_id)
        await record_event(
            "music_search_succeeded",
            trace_id=trace_id,
            payload={
                "query": query,
                "provider": diagnostic.provider,
                "title": diagnostic.title,
                "artist": diagnostic.artist,
                "duration_ms": diagnostic.duration * 1000 if diagnostic.duration is not None else None,
                "elapsed_ms": round((time.monotonic() - started_at) * 1000),
            },
        )
        return _success_payload(diagnostic, audio_url, metadata_url, [])

    providers = providers_from_env()
    if not providers:
        await record_event(
            "music_search_failed",
            trace_id=trace_id,
            payload={"query": query, "reason": "no_provider", "elapsed_ms": round((time.monotonic() - started_at) * 1000)},
        )
        return json.dumps(
            {
                "success": False,
                "message": "尚未启用可用音乐源，请检查 Navidrome、网易云、Fangpi 或 Jamendo 配置。",
                "provider_order": ["navidrome", "netease", "fangpi", "jamendo", "unofficial"],
            },
            ensure_ascii=False,
        )

    smart_result: MusicSearchResult | None = None
    if smart_search_enabled():
        smart_result = await SmartMusicSearch(providers, timeout=provider_timeout_seconds()).search(query)
        failures = list(smart_result.failures)
        track = smart_result.track
        if smart_result.status == "needs_confirmation" and smart_result.selected and track:
            diagnostics = _search_diagnostics(smart_result)
            await record_event(
                "music_search_confirmation_required",
                trace_id=trace_id,
                payload={
                    "query": query,
                    "title": track.title,
                    "artist": track.artist,
                    "provider": track.provider,
                    **diagnostics,
                    "elapsed_ms": round((time.monotonic() - started_at) * 1000),
                },
            )
            return json.dumps(
                {
                    "success": False,
                    "status": "needs_confirmation",
                    "message": f"找到的最接近结果是《{track.title}》 - {track.artist}，是否播放？",
                    "candidate": smart_result.selected.public_dict(),
                    "candidates": [candidate.public_dict() for candidate in smart_result.candidates[:3]],
                    "normalized_query": smart_result.query.normalized_query,
                    "fallback_failures": failures,
                },
                ensure_ascii=False,
            )
    else:
        track, failures = await ProviderChain(providers, timeout=provider_timeout_seconds()).search(query)
    if track is None:
        diagnostics = _search_diagnostics(smart_result) if smart_result else {}
        await record_event(
            "music_search_failed",
            trace_id=trace_id,
            payload={
                "query": query,
                "reason": "not_found",
                "failures": failures,
                **diagnostics,
                "elapsed_ms": round((time.monotonic() - started_at) * 1000),
            },
        )
        return json.dumps(
            {
                "success": False,
                "message": f"没有找到可播放的歌曲：{query}",
                "searched_providers": [provider.name for provider in providers],
                "fallback_failures": failures,
                "normalized_query": smart_result.query.normalized_query if smart_result else query,
                "candidates": [candidate.public_dict() for candidate in smart_result.candidates[:3]] if smart_result else [],
            },
            ensure_ascii=False,
        )

    try:
        audio_url, metadata_url = await register_proxy(track, trace_id)
    except RuntimeError as exc:
        await record_event(
            "music_search_failed",
            trace_id=trace_id,
            payload={
                "query": query,
                "reason": "proxy_registration_failed",
                "provider": track.provider,
                "title": track.title,
                "error": str(exc),
                "elapsed_ms": round((time.monotonic() - started_at) * 1000),
            },
        )
        return json.dumps(
            {
                "success": False,
                "message": str(exc),
                "provider": track.provider,
                "title": track.title,
            },
            ensure_ascii=False,
        )
    await record_event(
        "music_search_succeeded",
        trace_id=trace_id,
        payload={
            "query": query,
            "provider": track.provider,
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "duration_ms": track.duration * 1000 if track.duration is not None else None,
            "fallback_failures": failures,
            **(_search_diagnostics(smart_result) if smart_result else {}),
            "elapsed_ms": round((time.monotonic() - started_at) * 1000),
        },
    )
    return _success_payload(track, audio_url, metadata_url, failures)


if __name__ == "__main__":
    mcp.run(transport="stdio")
