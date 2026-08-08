#!/usr/bin/env python3
"""Unit tests for provider normalization, ordering, and configuration."""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from music_providers import (
    JamendoProvider,
    MusicProvider,
    NavidromeProvider,
    NeteaseProvider,
    ProviderChain,
    ProviderError,
    Track,
    providers_from_env,
)


class FakeProvider(MusicProvider):
    def __init__(self, name: str, tracks: list[Track] | None = None, error: str = ""):
        self.name = name
        self.tracks = tracks or []
        self.error = error
        self.calls = 0

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        self.calls += 1
        if self.error:
            raise ProviderError(self.error)
        return self.tracks


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_chain_stops_at_first_provider_with_results(self) -> None:
        local_track = Track("navidrome", "1", "本地歌曲", "歌手", "http://local/1.mp3")
        local = FakeProvider("navidrome", [local_track])
        jamendo = FakeProvider("jamendo", [Track("jamendo", "2", "公网歌曲", "Artist", "https://x/2.mp3")])

        resolved, failures = await ProviderChain([local, jamendo]).search("本地歌曲")

        self.assertEqual(resolved, local_track)
        self.assertEqual(failures, [])
        self.assertEqual(local.calls, 1)
        self.assertEqual(jamendo.calls, 0)

    async def test_chain_falls_back_after_error_and_empty_result(self) -> None:
        expected = Track("unofficial", "3", "目标歌曲", "歌手", "https://x/3.mp3")
        local = FakeProvider("navidrome", error="offline")
        jamendo = FakeProvider("jamendo")
        unofficial = FakeProvider("unofficial", [expected])

        resolved, failures = await ProviderChain([local, jamendo, unofficial]).search("目标歌曲")

        self.assertEqual(resolved, expected)
        self.assertEqual(failures, [{"provider": "navidrome", "reason": "offline"}])

    async def test_navidrome_response_becomes_mp3_stream(self) -> None:
        response = {
            "subsonic-response": {
                "status": "ok",
                "searchResult3": {
                    "song": [{"id": "song-1", "title": "海阔天空", "artist": "Beyond", "duration": 326}]
                },
            }
        }
        provider = NavidromeProvider("http://navidrome:4533", "user", "password")
        with patch("music_providers._get_json", return_value=response):
            tracks = await provider.search("海阔天空")

        self.assertEqual(tracks[0].provider, "navidrome")
        self.assertEqual(tracks[0].title, "海阔天空")
        self.assertIn("/rest/stream?", tracks[0].audio_url)
        self.assertIn("format=mp3", tracks[0].audio_url)
        self.assertNotIn("password", tracks[0].audio_url)

    async def test_jamendo_uses_stream_url(self) -> None:
        response = {
            "headers": {"status": "success"},
            "results": [{"id": "10", "name": "Hello", "artist_name": "World", "audio": "https://cdn/10.mp3"}],
        }
        provider = JamendoProvider("client-id")
        with patch("music_providers._get_json", return_value=response):
            tracks = await provider.search("Hello")

        self.assertEqual(tracks[0].audio_url, "https://cdn/10.mp3")

    async def test_netease_returns_only_native_complete_stream(self) -> None:
        responses = [
            {
                "code": 200,
                "result": {
                    "songs": [
                        {
                            "id": 347230,
                            "name": "海阔天空",
                            "ar": [{"name": "Beyond"}],
                            "al": {"name": "乐与怒", "picUrl": "https://img/cover.jpg"},
                            "dt": 326000,
                        },
                        {
                            "id": 347231,
                            "name": "不应继续解析",
                            "ar": [{"name": "Other"}],
                            "al": {},
                            "dt": 180000,
                        },
                    ]
                },
            },
            {
                "code": 200,
                "data": [
                    {
                        "id": 347230,
                        "url": "https://music.example/347230.mp3",
                        "type": "mp3",
                        "freeTrialInfo": None,
                    }
                ],
            },
        ]
        provider = NeteaseProvider("http://127.0.0.1:3000")
        with patch("music_providers._get_json", side_effect=responses) as get_json:
            tracks = await provider.search("海阔天空 Beyond")

        self.assertEqual(tracks[0].provider, "netease")
        self.assertEqual(tracks[0].artist, "Beyond")
        self.assertEqual(tracks[0].duration, 326)
        requested_urls = [call.args[0] for call in get_json.call_args_list]
        self.assertIn("/cloudsearch?", requested_urls[0])
        self.assertIn("/song/url/v1?", requested_urls[1])
        self.assertNotIn("unblock", "".join(requested_urls))
        self.assertEqual(get_json.call_count, 2)

    async def test_netease_accepts_and_marks_trial_stream(self) -> None:
        responses = [
            {
                "code": 200,
                "result": {"songs": [{"id": 1, "name": "试听歌曲", "ar": [], "al": {}, "dt": 30000}]},
            },
            {
                "code": 200,
                "data": [
                    {
                        "id": 1,
                        "url": "https://music.example/trial.mp3",
                        "freeTrialInfo": {"start": 0, "end": 30},
                    }
                ],
            },
        ]
        provider = NeteaseProvider("http://127.0.0.1:3000")
        with patch("music_providers._get_json", side_effect=responses):
            tracks = await provider.search("试听歌曲")

        self.assertEqual(len(tracks), 1)
        self.assertTrue(tracks[0].is_preview)

    def test_environment_preserves_requested_priority(self) -> None:
        env = {
            "NAVIDROME_URL": "http://localhost:4533",
            "NAVIDROME_USERNAME": "u",
            "NAVIDROME_PASSWORD": "p",
            "JAMENDO_CLIENT_ID": "j",
            "NETEASE_PROVIDER_ENABLED": "true",
            "NETEASE_API_URL": "http://localhost:3000",
            "UNOFFICIAL_PROVIDER_ENABLED": "true",
            "UNOFFICIAL_PROVIDER_URL": "http://localhost:9000/search",
            "MUSIC_PROVIDER_ORDER": "navidrome,netease,jamendo,unofficial",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual([provider.name for provider in providers_from_env()], list(env["MUSIC_PROVIDER_ORDER"].split(",")))


if __name__ == "__main__":
    unittest.main()
