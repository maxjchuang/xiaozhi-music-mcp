#!/usr/bin/env python3
"""Integration tests for the opaque LAN audio proxy."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

from mcp_pipe import AudioProxyServer, REGISTER_PATH


AUDIO = b"ID3-test-audio"
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
        else:
            body, content_type = AUDIO, "audio/mpeg"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, message_format: str, *args: object) -> None:
        pass


class AudioProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeAudioHandler)
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

    def test_manifest_lyrics_and_processed_artwork(self) -> None:
        upstream_base = self.upstream_url.rsplit("/", 1)[0]
        body = json.dumps(
            {
                "url": self.upstream_url,
                "title": "测试歌曲",
                "artist": "测试歌手",
                "album": "测试专辑",
                "duration_ms": 123000,
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

        with urlopen(manifest["lyrics"]["url"]) as response:
            self.assertEqual(response.read(), LYRICS)
        for key, expected_size in (("background_url", (360, 360)), ("disc_url", (192, 192))):
            with urlopen(manifest["artwork"][key]) as response:
                processed = Image.open(BytesIO(response.read()))
                self.assertEqual(processed.size, expected_size)


if __name__ == "__main__":
    unittest.main()
