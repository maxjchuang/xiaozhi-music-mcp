#!/usr/bin/env python3
"""Resolve music through prioritized providers for EchoEar playback."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Annotated, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastmcp import FastMCP
from pydantic import Field

from music_providers import ProviderChain, Track, providers_from_env


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


def resolve_diagnostic_track(query: str) -> Track | None:
    normalized = _normalize(query)
    candidates = (OFFICIAL_TEST_TRACK.title, *TEST_ALIASES)
    if any(_normalize(item) in normalized or normalized in _normalize(item) for item in candidates):
        return OFFICIAL_TEST_TRACK
    return None


def _register_proxy_sync(track: Track) -> tuple[str, str]:
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


async def register_proxy(track: Track) -> tuple[str, str]:
    return await asyncio.to_thread(_register_proxy_sync, track)


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
    return json.dumps(
        {
            "success": True,
            **track.public_dict(),
            "audio_url": audio_url,
            "fallback_failures": failures,
            "next_step": "立即调用设备端 MCP 工具 self.online_music.play_music",
            "device_tool": "self.online_music.play_music",
            "metadata_url": metadata_url or None,
            "device_arguments": device_arguments,
        },
        ensure_ascii=False,
    )


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
    if diagnostic := resolve_diagnostic_track(query):
        audio_url, metadata_url = await register_proxy(diagnostic)
        return _success_payload(diagnostic, audio_url, metadata_url, [])

    providers = providers_from_env()
    if not providers:
        return json.dumps(
            {
                "success": False,
                "message": "尚未启用可用音乐源，请检查 Navidrome、网易云、Fangpi 或 Jamendo 配置。",
                "provider_order": ["navidrome", "netease", "fangpi", "jamendo", "unofficial"],
            },
            ensure_ascii=False,
        )

    track, failures = await ProviderChain(providers, timeout=provider_timeout_seconds()).search(query)
    if track is None:
        return json.dumps(
            {
                "success": False,
                "message": f"没有找到可播放的歌曲：{query}",
                "searched_providers": [provider.name for provider in providers],
                "fallback_failures": failures,
            },
            ensure_ascii=False,
        )

    try:
        audio_url, metadata_url = await register_proxy(track)
    except RuntimeError as exc:
        return json.dumps(
            {
                "success": False,
                "message": str(exc),
                "provider": track.provider,
                "title": track.title,
            },
            ensure_ascii=False,
        )
    return _success_payload(track, audio_url, metadata_url, failures)


if __name__ == "__main__":
    mcp.run(transport="stdio")
