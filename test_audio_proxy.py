#!/usr/bin/env python3
"""Integration tests for the opaque LAN audio proxy."""

from __future__ import annotations

from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

from mcp_pipe import (
    AudioProxyServer,
    CACHE_LOOKUP_PATH,
    MAX_IMAGE_PIXELS,
    REGISTER_PATH,
)
from usage_analytics import AnalyticsRecorder, AnalyticsStore


AUDIO = b"ID3-test-audio"
SLOW_AUDIO = b"ID3" + bytes(range(256)) * 512
LYRICS = "[00:01.00]测试歌词".encode()


def jpeg_fixture() -> bytes:
    output = BytesIO()
    Image.new("RGB", (640, 480), (180, 30, 60)).save(output, "JPEG")
    return output.getvalue()


COVER = jpeg_fixture()


class FakeAudioHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/cover.jpg":
            body, content_type = COVER, "image/jpeg"
        elif self.path == "/lyrics.lrc":
            body, content_type = LYRICS, "text/plain"
        elif self.path == "/slow.mp3":
            body, content_type = SLOW_AUDIO, "audio/mpeg"
        else:
            body, content_type = AUDIO, "audio/mpeg"
        if content_type == "audio/mpeg":
            self.server.audio_requests += 1  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.path == "/slow.mp3":
            for offset in range(0, len(body), 4096):
                self.wfile.write(body[offset : offset + 4096])
                self.wfile.flush()
                time.sleep(0.002)
        else:
            self.wfile.write(body)

    def log_message(self, message_format: str, *args: object) -> None:
        pass


class AudioProxyTests(unittest.TestCase):
    def test_artwork_limit_accepts_common_netease_master_size(self) -> None:
        self.assertGreaterEqual(MAX_IMAGE_PIXELS, 4167 * 4167)

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.cache_environment = patch.dict(
            os.environ,
            {
                "MUSIC_CACHE_ENABLED": "true",
                "MUSIC_CACHE_DIR": str(Path(self.temporary_directory.name) / "music-cache"),
                "MUSIC_CACHE_MAX_BYTES": str(50 * 1024**3),
            },
        )
        self.cache_environment.start()
        self.analytics_store = AnalyticsStore(
            Path(self.temporary_directory.name) / "analytics.sqlite3"
        )
        self.recorder_patch = patch(
            "mcp_pipe.get_recorder", return_value=AnalyticsRecorder(self.analytics_store)
        )
        self.recorder_patch.start()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeAudioHandler)
        self.upstream.audio_requests = 0  # type: ignore[attr-defined]
        upstream_port = self.upstream.server_address[1]
        self.upstream_url = f"http://127.0.0.1:{upstream_port}/song.mp3"
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()

        self.proxy = AudioProxyServer(("127.0.0.1", 0), "http://placeholder", "test-secret")
        proxy_port = self.proxy.server_address[1]
        self.proxy.public_base_url = f"http://127.0.0.1:{proxy_port}"
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()

    def tearDown(self) -> None:
        self.proxy.shutdown()
        self.proxy.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.recorder_patch.stop()
        self.cache_environment.stop()
        self.temporary_directory.cleanup()

    def _register(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "url": self.upstream_url,
            "title": "测试歌曲",
            "artist": "测试歌手",
            "provider": "test",
            "track_id": "track-1",
            "query": "播放测试歌曲",
        }
        payload.update(overrides)
        body = json.dumps(payload).encode()
        request = Request(
            self.proxy.public_base_url + REGISTER_PATH,
            data=body,
            method="POST",
            headers={"Authorization": "Bearer test-secret", "Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            return json.load(response)

    def _lookup_cache(self, query: str) -> dict[str, object]:
        body = json.dumps({"query": query, "trace_id": "cache-trace"}).encode()
        request = Request(
            self.proxy.public_base_url + CACHE_LOOKUP_PATH,
            data=body,
            method="POST",
            headers={"Authorization": "Bearer test-secret", "Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            return json.load(response)

    def _wait_for_cache(self, query: str, timeout: float = 2) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self._lookup_cache(query)
            except HTTPError as exc:
                if exc.code != 404 or time.monotonic() >= deadline:
                    raise
                exc.close()
                time.sleep(0.02)

    def test_register_and_stream_without_exposing_upstream(self) -> None:
        body = json.dumps({"url": self.upstream_url, "title": "测试歌曲"}).encode()
        register = Request(
            self.proxy.public_base_url + REGISTER_PATH,
            data=body,
            method="POST",
            headers={"Authorization": "Bearer test-secret", "Content-Type": "application/json"},
        )
        with urlopen(register) as response:
            public_url = json.load(response)["url"]

        self.assertNotIn(self.upstream_url, public_url)
        with urlopen(public_url) as response:
            self.assertEqual(response.read(), AUDIO)
            self.assertEqual(response.headers.get_content_type(), "audio/mpeg")

    def test_registration_requires_local_secret(self) -> None:
        body = json.dumps({"url": self.upstream_url}).encode()
        register = Request(self.proxy.public_base_url + REGISTER_PATH, data=body, method="POST")
        with self.assertRaises(HTTPError) as raised:
            urlopen(register)
        self.assertEqual(raised.exception.code, 401)
        raised.exception.close()

    def test_played_music_is_cached_as_standalone_mp3_and_served_with_range(self) -> None:
        registered = self._register()
        with urlopen(str(registered["url"])) as response:
            self.assertEqual(response.read(), AUDIO)
        upstream_requests = self.upstream.audio_requests  # type: ignore[attr-defined]

        cached = self._wait_for_cache("播放 测试歌曲")
        track = cached["track"]
        assert isinstance(track, dict)
        audio_path = Path(str(track["audio_path"]))
        self.assertEqual(audio_path.suffix, ".mp3")
        self.assertEqual(audio_path.read_bytes(), AUDIO)

        ranged = Request(str(cached["url"]), headers={"Range": "bytes=4-8"})
        with urlopen(ranged) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), AUDIO[4:9])
        self.assertEqual(self.upstream.audio_requests, upstream_requests)  # type: ignore[attr-defined]

        event_types = [item.event.event_type for item in self.analytics_store.claim_batch()]
        self.assertIn("music_cache_saved", event_types)

    def test_client_disconnect_still_finishes_complete_mp3_cache(self) -> None:
        registered = self._register(url=self.upstream_url.replace("song.mp3", "slow.mp3"))
        response = urlopen(str(registered["url"]))
        self.assertEqual(response.read(32), SLOW_AUDIO[:32])
        response.close()

        cached = self._wait_for_cache("播放测试歌曲", timeout=3)
        track = cached["track"]
        assert isinstance(track, dict)
        self.assertEqual(Path(str(track["audio_path"])).read_bytes(), SLOW_AUDIO)

    def test_preview_playback_is_not_cached(self) -> None:
        registered = self._register(playback_access="preview")
        with urlopen(str(registered["url"])) as response:
            self.assertEqual(response.read(), AUDIO)
        body = json.dumps({"query": "播放测试歌曲"}).encode()
        lookup = Request(
            self.proxy.public_base_url + CACHE_LOOKUP_PATH,
            data=body,
            method="POST",
            headers={"Authorization": "Bearer test-secret", "Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(lookup)
        self.assertEqual(raised.exception.code, 404)
        raised.exception.close()

    def test_manifest_lyrics_and_processed_artwork(self) -> None:
        upstream_base = self.upstream_url.rsplit("/", 1)[0]
        body = json.dumps(
            {
                "url": self.upstream_url,
                "title": "测试歌曲",
                "artist": "测试歌手",
                "album": "测试专辑",
                "duration_ms": 123000,
                "song_duration_ms": 234000,
                "playback_access": "preview",
                "trace_id": "trace-test",
                "artwork_url": upstream_base + "/cover.jpg",
                "lyrics_url": upstream_base + "/lyrics.lrc",
            }
        ).encode()
        register = Request(
            self.proxy.public_base_url + REGISTER_PATH,
            data=body,
            method="POST",
            headers={"Authorization": "Bearer test-secret", "Content-Type": "application/json"},
        )
        with urlopen(register) as response:
            registered = json.load(response)

        with urlopen(registered["metadata_url"]) as response:
            manifest = json.load(response)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["title"], "测试歌曲")
        self.assertEqual(manifest["duration_ms"], 123000)
        self.assertEqual(manifest["song_duration_ms"], 234000)
        self.assertEqual(manifest["playback_access"], "preview")
        self.assertEqual(manifest["trace_id"], "trace-test")
        self.assertTrue(manifest["telemetry_url"].endswith("/telemetry"))
        self.assertEqual(manifest["lyrics"]["offset_ms"], 0)

        with urlopen(manifest["lyrics"]["url"]) as response:
            self.assertEqual(response.read(), LYRICS)
        for key, expected_size in (("background_url", (360, 360)), ("disc_url", (192, 192))):
            with urlopen(manifest["artwork"][key]) as response:
                processed = Image.open(BytesIO(response.read()))
                self.assertEqual(processed.size, expected_size)

        self.assertNotIn("animation", manifest["artwork"])

    def test_playback_telemetry_is_idempotent_and_projects_terminal_state(self) -> None:
        body = json.dumps(
            {
                "url": self.upstream_url,
                "title": "测试歌曲",
                "artist": "测试歌手",
                "duration_ms": 120000,
                "song_duration_ms": 240000,
                "playback_access": "preview",
                "provider": "netease",
                "trace_id": "trace-1",
            }
        ).encode()
        register = Request(
            self.proxy.public_base_url + REGISTER_PATH,
            data=body,
            method="POST",
            headers={"Authorization": "Bearer test-secret", "Content-Type": "application/json"},
        )
        with urlopen(register) as response:
            metadata_url = json.load(response)["metadata_url"]
        with urlopen(metadata_url) as response:
            telemetry_url = json.load(response)["telemetry_url"]

        telemetry = {
            "schema_version": 1,
            "device_id": "echoear-raw-id",
            "session_id": "session-1",
            "events": [
                {
                    "event_id": "boot-1:1",
                    "event_type": "playback_started",
                    "playback_id": "boot-1:playback-1",
                    "monotonic_ms": 1000,
                    "payload": {"audible_played_ms": 0},
                },
                {
                    "event_id": "boot-1:2",
                    "event_type": "audio_underrun",
                    "playback_id": "boot-1:playback-1",
                    "monotonic_ms": 5000,
                    "payload": {"wait_ms": 80},
                },
                {
                    "event_id": "boot-1:3",
                    "event_type": "playback_stopped",
                    "playback_id": "boot-1:playback-1",
                    "monotonic_ms": 8000,
                    "payload": {
                        "audible_played_ms": 7000,
                        "elapsed_since_start_ms": 7000,
                        "end_reason": "user_stopped",
                    },
                },
                {
                    "event_id": "boot-1:4",
                    "event_type": "user_utterance",
                    "session_id": "session-stop",
                    "monotonic_ms": 8100,
                    "payload": {"user_text": "换一首歌"},
                },
            ],
        }
        request = Request(
            telemetry_url,
            data=json.dumps(telemetry).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            result = json.load(response)
        self.assertEqual(result, {"accepted": 4, "duplicates": 0})
        with urlopen(request) as response:
            duplicate = json.load(response)
        self.assertEqual(duplicate, {"accepted": 0, "duplicates": 4})

        playback = self.analytics_store.get_playback("boot-1:playback-1")
        assert playback is not None
        self.assertEqual(playback["trace_id"], "trace-1")
        self.assertEqual(playback["playback_access"], "preview")
        self.assertEqual(playback["audible_played_ms"], 7000)
        self.assertEqual(playback["end_reason"], "user_stopped")
        self.assertEqual(playback["session_id"], "session-1")
        self.assertEqual(playback["quick_skip_level"], "immediate")
        self.assertEqual(playback["underrun_count"], 1)
        self.assertEqual(playback["underrun_total_ms"], 80)
        started = datetime.fromisoformat(playback["started_at"])
        ended = datetime.fromisoformat(playback["ended_at"])
        self.assertAlmostEqual((ended - started).total_seconds(), 7.0, places=2)
        session = self.analytics_store.get_session("session-1")
        assert session is not None
        self.assertEqual(session["result"], "stopped")
        self.assertEqual(session["user_text"], "")
        stop_session = self.analytics_store.get_session("session-stop")
        assert stop_session is not None
        self.assertEqual(stop_session["user_text"], "换一首歌")
        self.assertEqual(stop_session["turn_count"], 1)


if __name__ == "__main__":
    unittest.main()
