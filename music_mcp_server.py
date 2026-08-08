#!/usr/bin/env python3
"""Resolve approved audio URLs for EchoEar's device-side music MCP tool."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field


mcp = FastMCP("xiaozhi-music-resolver")

# Keep the first end-to-end test deterministic and copyright-safe. This file is
# hosted by Espressif and is used as an HTTP/MP3 playback test asset.
TEST_TRACKS: tuple[dict[str, Any], ...] = (
    {
        "id": "espressif-stereo-44100",
        "title": "乐鑫官方立体声测试音频",
        "artist": "Espressif",
        "audio_url": "https://dl.espressif.com/dl/audio/ff-16b-2c-44100hz.mp3",
        "content_type": "audio/mpeg",
        "aliases": (
            "乐鑫官方测试音频",
            "乐鑫测试音频",
            "官方测试音频",
            "测试音频",
            "测试歌曲",
            "espressif test audio",
        ),
    },
)


def _normalize(value: str) -> str:
    return "".join(value.casefold().split())


def resolve_track(query: str) -> dict[str, Any] | None:
    """Resolve a query against the deliberately small approved catalogue."""
    normalized_query = _normalize(query)
    if not normalized_query:
        return None

    for track in TEST_TRACKS:
        candidates = (track["title"], track["artist"], *track["aliases"])
        if any(
            _normalize(candidate) in normalized_query
            or normalized_query in _normalize(candidate)
            for candidate in candidates
        ):
            return track
    return None


@mcp.tool()
async def resolve_music_url(
    query: Annotated[
        str,
        Field(
            min_length=1,
            description="要解析的歌曲或测试音频名称，例如：乐鑫官方测试音频",
        ),
    ],
) -> str:
    """解析白名单音频的直链。

    本工具只解析 URL，不播放音频。成功后必须继续调用设备端工具
    `self.online_music.play_music`，并原样使用返回的 `device_arguments`。
    不要改用官方 `play_music`、`search_music` 或 `self.music.play_song`。
    """
    track = resolve_track(query)
    if track is None:
        return json.dumps(
            {
                "success": False,
                "message": "当前仅开放乐鑫官方测试音频，请让用户说“播放乐鑫官方测试音频”。",
                "available_tracks": [item["title"] for item in TEST_TRACKS],
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "success": True,
            "title": track["title"],
            "artist": track["artist"],
            "audio_url": track["audio_url"],
            "content_type": track["content_type"],
            "next_step": "立即调用设备端 MCP 工具 self.online_music.play_music",
            "device_tool": "self.online_music.play_music",
            "device_arguments": {
                "play_type": "url",
                "url": track["audio_url"],
                "url_song_name": track["title"],
            },
        },
        ensure_ascii=False,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
