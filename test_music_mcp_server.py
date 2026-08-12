#!/usr/bin/env python3
"""Unit tests for resolver-to-device handoff payloads."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from music_mcp_server import _register_proxy_sync, _success_payload
from music_providers import Track


class MusicMcpServerTests(unittest.TestCase):
    def test_success_payload_adds_optional_metadata_url(self) -> None:
        track = Track("netease", "1", "小苹果", "筷子兄弟", "https://upstream/song.mp3")
        payload = json.loads(
            _success_payload(
                track,
                "http://192.168.1.2:8765/media/token/audio",
                "http://192.168.1.2:8765/media/token/manifest.json",
                [],
            )
        )

        self.assertEqual(
            payload["device_arguments"]["metadata_url"],
            payload["metadata_url"],
        )

    def test_direct_mode_keeps_legacy_device_arguments(self) -> None:
        track = Track("diagnostic", "1", "测试", "Espressif", "https://upstream/test.mp3")
        payload = json.loads(_success_payload(track, track.audio_url, "", []))

        self.assertNotIn("metadata_url", payload["device_arguments"])

    def test_registration_sends_private_metadata_without_exposing_it_publicly(self) -> None:
        track = Track(
            "unofficial",
            "1",
            "歌曲",
            "歌手",
            "https://upstream/song.mp3",
            lyrics="[00:01.00]歌词",
            lyrics_url="https://upstream/song.lrc",
        )

        with patch.dict(
            "os.environ",
            {"MUSIC_PROXY_REGISTER_URL": "http://127.0.0.1/register", "MUSIC_PROXY_REGISTER_TOKEN": "secret"},
            clear=False,
        ), patch("music_mcp_server.urlopen") as urlopen:
            response = urlopen.return_value.__enter__.return_value
            response.read.return_value = b'{"url":"http://lan/audio","metadata_url":"http://lan/manifest.json"}'
            response.__iter__.return_value = iter(response.read.return_value.splitlines(True))
            audio_url, metadata_url = _register_proxy_sync(track)

        request = urlopen.call_args.args[0]
        registration = json.loads(request.data)
        self.assertEqual(registration["lyrics"], track.lyrics)
        self.assertEqual(audio_url, "http://lan/audio")
        self.assertEqual(metadata_url, "http://lan/manifest.json")
        self.assertNotIn("lyrics", track.public_dict())


if __name__ == "__main__":
    unittest.main()
